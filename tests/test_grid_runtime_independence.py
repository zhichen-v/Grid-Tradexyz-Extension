"""Grid startup must remain independent of the removed Market Maker runtime."""

import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class GridRuntimeIndependenceTests(unittest.TestCase):
    def test_grid_imports_and_cli_help_do_not_load_market_maker(self):
        probe = textwrap.dedent("""
            import contextlib
            import io
            import runpy
            import socket
            import sys

            class NoMarketMaker:
                def find_spec(self, fullname, path=None, target=None):
                    if any(
                        part.startswith(('market_maker', 'volume_maker'))
                        or part in {'run_market_maker', 'run_volume_market_maker'}
                        for part in fullname.split('.')
                    ):
                        raise AssertionError('Grid imported removed runtime: ' + fullname)

            def no_network(*args, **kwargs):
                raise AssertionError('Grid import/help attempted network access')

            socket.socket.connect = no_network
            socket.create_connection = no_network
            sys.meta_path.insert(0, NoMarketMaker())
            sys.path.insert(0, sys.argv[1])
            import run_grid_trading
            from core.services.grid.coordinator import GridCoordinator
            from core.services.grid.implementations import GridEngineImpl, GridStrategyImpl
            from core.services.grid.terminal_ui import GridTerminalUI

            for flag in ('--help', '--version'):
                sys.argv = ['run_grid_trading.py', flag]
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    try:
                        runpy.run_path(run_grid_trading.__file__, run_name='__main__')
                    except SystemExit as exc:
                        assert exc.code == 0, exc.code
                    else:
                        raise AssertionError('CLI did not exit before strategy startup')
                assert 'Grid' in output.getvalue(), output.getvalue()
        """)
        with tempfile.TemporaryDirectory(prefix="grid-import-check-") as scratch:
            result = subprocess.run(
                [sys.executable, "-B", "-c", probe, str(REPO_ROOT)],
                cwd=scratch,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_retained_production_has_no_market_maker_imports(self):
        paths = [*REPO_ROOT.glob("*.py")]
        for directory in ("core", "scripts"):
            paths.extend((REPO_ROOT / directory).rglob("*.py"))
        violations = []
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imports = [node.module or "", *(alias.name for alias in node.names)]
                else:
                    continue
                if any(
                    part.startswith(("market_maker", "volume_maker"))
                    or part in {"run_market_maker", "run_volume_market_maker"}
                    for name in imports for part in name.split(".")
                ):
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
