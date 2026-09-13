"""The actual PowerShell launcher, isolated files, real console signals, no venue network."""

import json
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from tests.mm_v2_windows_process import WindowsConsoleProcess, clean_environment


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/Scripts/python.exe"
POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


class LauncherWorkspace:
    """Only explicit source files and synthetic settings enter this workspace."""

    def __init__(self):
        import _winapi
        self.temporary = TemporaryDirectory(prefix="mm launcher 測試 ")
        self.path = Path(self.temporary.name)
        self.links = []
        try:
            for name in ("run_live_test.ps1", "run_volume_market_maker.py", "lighter_preflight.py"):
                shutil.copyfile(ROOT / name, self.path / name)
            for name in ("core", ".venv"):
                destination = self.path / name
                _winapi.CreateJunction(str(ROOT / name), str(destination))
                self.links.append(destination)
            (self.path / "logs").mkdir()
            (self.path / "config/market_maker_v2").mkdir(parents=True)
            shutil.copyfile(ROOT / "config/market_maker_v2/lighter_btc_volume.example.yaml",
                self.path / "config/market_maker_v2/test_live_economics_60m.yaml")
            self.env = clean_environment(self.path / "profile")
        except BaseException:
            self.close()
            raise

    def start(self):
        return WindowsConsoleProcess([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", self.path / "run_live_test.ps1"],
            cwd=self.path.parent, env=self.env,
            stdout=self.path / "stdout.txt", stderr=self.path / "stderr.txt")

    def configure_wire(self, *, duration_seconds):
        from tests.mm_v2_wire_fixture import WireFixture
        paths = WireFixture(time_mode="real").write_synthetic_workspace(self.path, duration_seconds)
        bootstrap = self.path / "bootstrap"
        bootstrap.mkdir()
        # Loaded by both the configuration-validator Python and the real CLI.
        # Neither an unavailable bootstrap nor a failed guard can fall through
        # into real adapter construction. Only synthetic files are in this cwd.
        (bootstrap / "sitecustomize.py").write_text('''import atexit, json, os, sys
from pathlib import Path
try:
    from mm_v2_wire_fixture import WireFixture
    from mm_v2_wire_evidence import production_calls, call_evidence
    workspace = Path(os.environ["MM_V2_WIRE_WORKSPACE"])
    phase = "validate" if sys.argv[0] == "-c" else "run"
    fixture = WireFixture(scenario="normal", time_mode="real", state_path=workspace / (phase + "-venue.json"))
    fixture_context = fixture.install()
    fixture_context.__enter__()
    trace_context = production_calls()
    calls = trace_context.__enter__()
    def completed():
        fixture.venue.persist()
        trace = {"synthetic": True, "pid": os.getpid(), "python": sys.executable,
                 "calls": call_evidence(calls)}
        (workspace / (phase + "-trace.json")).write_text(json.dumps(trace), encoding="utf-8")
    atexit.register(completed)
    (workspace / (phase + "-bootstrap.json")).write_text(json.dumps({"pid": os.getpid(), "python": sys.executable}), encoding="utf-8")
except BaseException:
    os._exit(97)
''', encoding="utf-8")
        self.env.update(MM_V2_WIRE_WORKSPACE=str(self.path),
            PYTHONPATH=os.pathsep.join((str(bootstrap), str(self.path), str(ROOT / "tests"))))
        return paths

    def wait_for(self, predicate, process, *, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            if process.poll() is not None:
                raise AssertionError(f"launcher exited before readiness: code={process.poll()}")
            time.sleep(0.02)
        raise AssertionError("launcher readiness deadline exceeded")

    def windows(self):
        return [json.loads(path.read_text(encoding="utf-8-sig"))
                for path in (self.path / "logs").glob("*.window.json")]

    def close(self):
        for path in reversed(self.links):
            if path.parent != self.path or not path.is_junction():
                raise RuntimeError("unexpected temporary workspace junction")
            os.rmdir(path)  # Remove only the link, never the source directory.
        self.links.clear()
        self.temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@unittest.skipUnless(os.name == "nt", "Windows launcher contract")
class LauncherTests(unittest.TestCase):
    def _full_wire_case(self, stop):
        from core.services.market_maker_v2.config import load_config
        from tests.mm_v2_wire_evidence import source_fingerprint, write_evidence
        seconds = 30 if stop else 3
        source_at_start = source_fingerprint(ROOT)
        with LauncherWorkspace() as workspace:
            paths = workspace.configure_wire(duration_seconds=seconds)
            expected_config = load_config(paths["config"])
            def journal():
                candidates = list((workspace.path / "logs").glob("mm_v2_economics_*.jsonl"))
                if not candidates:
                    return []
                try:
                    return [json.loads(line) for line in candidates[0].read_text(encoding="utf-8").splitlines()]
                except json.JSONDecodeError:
                    return []  # The writer may be between write and flush.
            started = time.monotonic()
            stopped = None
            with workspace.start() as process:
                if stop:
                    workspace.wait_for(lambda: len([r for r in journal() if r["event"] == "order_evidence"]) >= 2,
                        process, timeout=100)
                    stopped = time.monotonic()
                    process.send_ctrl_c()
                code = process.wait(60 if stop else 110)
                ended = time.monotonic()
                self.assertEqual(code, 0, (workspace.path / "stderr.txt").read_text(encoding="utf-8", errors="replace")[-1000:])
                self.assertEqual(process.active_processes(), 0)
                self.assertFalse(process.watchdog_used)
                rows = journal()
                self.assertEqual(rows[0]["event"], "account_snapshot")
                self.assertEqual(rows[-1]["event"], "session_report")
                venue = json.loads((workspace.path / "run-venue.json").read_text(encoding="utf-8"))
                self.assertEqual(Decimal(venue["position"]), Decimal(0))
                self.assertEqual(venue["open_order_ids"], [])
                accounts = [row["data"] for row in rows if row["event"] == "account_snapshot"]
                self.assertEqual(Decimal(accounts[-1]["equity"]), Decimal(venue["cash"]))
                self.assertTrue(accounts[-1]["authenticated"])
                self.assertEqual(Decimal(accounts[-1]["position"]), Decimal(0))
                self.assertEqual(accounts[-1]["open_order_count"], 0)
                exits = [row["data"] for row in rows if row["event"] == "bounded_exit"]
                self.assertEqual(len(exits), 1)
                self.assertEqual(exits[0]["status"], "flat")
                session_start = accounts[0]["observed_monotonic"]
                exit_start = stopped if stop else session_start + seconds
                self.assertLessEqual(exits[0]["observed_monotonic"] - exit_start, 30)
                active_duration = Decimal(rows[-1]["data"]["duration_seconds"])
                if stop:
                    self.assertLess(active_duration, Decimal(seconds))
                else:
                    self.assertGreaterEqual(active_duration, Decimal(seconds))
                traces = json.loads((workspace.path / "run-trace.json").read_text(encoding="utf-8"))
                self.assertEqual(Path(traces["python"]).resolve(), PYTHON.resolve())
                observed = {(row["module"], row["qualname"]) for row in traces["calls"]}
                for call in (("__main__", "main"), ("lighter_preflight", "load_settings"),
                             ("lighter_preflight", "build_adapter"),
                             ("core.services.market_maker_v2.orchestrator", "VolumeSession.run"),
                             ("lighter.signer_client", "SignerClient.create_order"),
                             ("lighter.signer_client", "SignerClient.cancel_order"),
                             ("lighter.api_client", "ApiClient.response_deserialize")):
                    self.assertIn(call, observed)
                outputs = list((workspace.path / "logs").glob("mm_v2_economics_*.jsonl"))
                self.assertEqual(len(outputs), 1)
                output = outputs[0]
                budget = json.loads(Path(str(output) + ".budget.json").read_text(encoding="utf-8"))
                window = workspace.windows()[0]
                self.assertEqual(window["exit_code"], 0)
                self.assertEqual(window["planned_seconds"], seconds)
                self.assertEqual(budget["provenance"]["effective_config"]["session"]["duration_seconds"], seconds)
                self.assertGreaterEqual(ended - started, 60)  # The actual startup quarantine was not skipped.
                self.assertLess(abs(window["wall_seconds"] - (ended - started)), 5)
                summaries = []
                for line in (workspace.path / "stdout.txt").read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict) and "completed" in value:
                        summaries.append(value)
                self.assertEqual(len(summaries), 1)
                summary = summaries[0]
                self.assertTrue(summary["completed"])
                self.assertTrue(summary["final_authenticated"])
                self.assertEqual(Decimal(summary["final_position"]), Decimal(0))
                self.assertEqual(summary["final_open_orders"], 0)
                cancels = [event["order_id"] for event in venue["events"] if event["kind"] == "cancel_accepted"]
                self.assertEqual(Counter(cancels), {identifier: 1 for identifier in cancels})
                if stop:
                    self.assertLess(ended - stopped, 60)
                    self.assertFalse([event for event in venue["events"] if event["kind"] == "create_accepted"
                                      and event["time_in_force"] == "post-only" and event["monotonic"] > stopped])
                target = ROOT / "logs" / ("mm_v2_launcher_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
                                         + ("_ctrl_c" if stop else "_deadline"))
                target.mkdir(parents=True)
                for suffix in ("", ".budget.json", ".window.json"):
                    shutil.copyfile(Path(str(output) + suffix), target / ("session.jsonl" + suffix))
                shutil.copyfile(workspace.path / "run-venue.json", target / "venue.json")
                manifest = write_evidence(target / "session.synthetic.json", root=ROOT, config=expected_config,
                    scenario="normal", time_mode="real", venue=venue, exit_code=code,
                    source_at_start=source_at_start, loaded_code=traces["calls"],
                    process={"powershell_pid": process.pid, "python_pid": traces["pid"], "exit_code": code,
                             "started_monotonic": started, "stop_monotonic": stopped, "ended_monotonic": ended,
                             "watchdog_used": False, "remaining_processes": 0})
                self.assertTrue(manifest["synthetic"])
                self.assertFalse(manifest["live_economics_evidence"])
                return str(target)

    def test_original_launcher_and_full_wire_session_deadline_and_ctrl_c(self):
        # The consoles, synthetic venues and temporary settings are independent;
        # both keep the real 60-second startup quarantine and monotonic clock.
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self._full_wire_case, stop) for stop in (False, True)]
            self.assertEqual(len([future.result(timeout=160) for future in futures]), 2)

    def test_startup_failures_do_not_reach_child_or_report_success(self):
        for failure in ("missing_config", "invalid_config", "missing_python"):
            with self.subTest(failure=failure), LauncherWorkspace() as workspace:
                (workspace.path / "run_volume_market_maker.py").write_text(
                    "from pathlib import Path\nPath('unexpected-start').touch()\n", encoding="utf-8")
                config = workspace.path / "config/market_maker_v2/test_live_economics_60m.yaml"
                if failure == "missing_config":
                    config.unlink()
                elif failure == "invalid_config":
                    config.write_text("market_maker_v2: invalid\n", encoding="utf-8")
                else:
                    link = workspace.path / ".venv"
                    os.rmdir(link)
                    workspace.links.remove(link)
                with workspace.start() as process:
                    self.assertEqual(process.wait(10), 1)
                    self.assertFalse((workspace.path / "unexpected-start").exists())
                    self.assertEqual(workspace.windows(), [])
                    self.assertEqual(process.active_processes(), 0)
                    self.assertFalse(process.watchdog_used)

    def test_launcher_exit_code_reports_actual_child_outcome(self):
        for expected in (0, 7):
            with self.subTest(expected=expected), LauncherWorkspace() as workspace:
                (workspace.path / "run_volume_market_maker.py").write_text(
                    f"raise SystemExit({expected})\n", encoding="utf-8")
                with workspace.start() as process:
                    self.assertEqual(process.wait(10), expected)
                    self.assertEqual(workspace.windows()[0]["exit_code"], expected)
                    self.assertEqual(process.active_processes(), 0)
                    self.assertFalse(process.watchdog_used)
                    with self.assertRaisesRegex(RuntimeError, "already exited"):
                        process.send_ctrl_c()

    def test_hidden_console_delivers_ctrl_c_to_child_and_runs_launcher_finally(self):
        # A small standard-library child isolates console semantics before the
        # same harness is used for the complete production entry point.
        script = '''import json, signal, sys, time
from pathlib import Path
stop = False
def request_stop(signum, frame):
    global stop
    stop = True
signal.signal(signal.SIGINT, request_stop)
Path("ready.json").write_text(json.dumps({"python": sys.executable}))
try:
    while not stop:
        time.sleep(0.02)
finally:
    time.sleep(0.4)  # Cleanup continues after the console event reaches PowerShell.
    Path("child-final.json").write_text(json.dumps({"stop": stop}))
'''
        with LauncherWorkspace() as workspace:
            (workspace.path / "run_volume_market_maker.py").write_text(script, encoding="utf-8")
            with workspace.start() as process:
                workspace.wait_for(lambda: (workspace.path / "ready.json").exists(), process)
                process.send_ctrl_c()
                code = process.wait(10)
                final = json.loads((workspace.path / "child-final.json").read_text())
                self.assertTrue(final["stop"])
                self.assertFalse(process.watchdog_used)
                self.assertEqual(process.active_processes(), 0)
                self.assertEqual(len(workspace.windows()), 1)
                self.assertEqual(workspace.windows()[0]["exit_code"], code)
                self.assertEqual(code, 0)

    def test_watchdog_terminates_the_entire_owned_process_tree(self):
        with LauncherWorkspace() as workspace:
            script = '''import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path("ready.json").write_text(str(child.pid))
time.sleep(60)
'''
            (workspace.path / "run_volume_market_maker.py").write_text(script, encoding="utf-8")
            with workspace.start() as process:
                workspace.wait_for(lambda: (workspace.path / "ready.json").exists(), process)
                self.assertGreaterEqual(process.active_processes(), 3)
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(0.05)
                self.assertTrue(process.watchdog_used)
                deadline = time.monotonic() + 5
                while process.active_processes() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(process.active_processes(), 0)


if __name__ == "__main__":
    unittest.main()
