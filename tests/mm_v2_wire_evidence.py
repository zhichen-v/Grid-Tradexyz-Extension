"""Reproducibility evidence for synthetic V2 integration tests, never live PnL."""

from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal
import hashlib
from importlib import metadata
import json
import marshal
from pathlib import Path
import platform
import sys


@contextmanager
def production_calls():
    """Observe original Python frames without replacing any production result."""
    counts = Counter()
    counts.codes = defaultdict(set)
    previous = sys.getprofile()

    def observe(frame, event, _arg):
        if event != "call":
            return
        module = frame.f_globals.get("__name__", "")
        entrypoint = module == "__main__" and frame.f_code.co_filename.endswith("run_volume_market_maker.py")
        if entrypoint or module.startswith(("core.", "lighter.", "run_volume_market_maker", "lighter_preflight")):
            key = (module, frame.f_code.co_qualname)
            counts[key] += 1
            counts.codes[key].add(frame.f_code)

    sys.setprofile(observe)
    try:
        yield counts
    finally:
        sys.setprofile(previous)


def call_evidence(calls, *, selected_codes=None):
    return [{"module": module, "qualname": name, "path": code.co_filename,
             "sha256": hashlib.sha256(marshal.dumps(code)).hexdigest(), "calls": calls[(module, name)]}
            for (module, name), codes in sorted(calls.codes.items())
            for code in codes if selected_codes is None or code in selected_codes]


def _canonical(value):
    def decimal_only(item):
        if type(item) is Decimal:
            return str(item)
        raise TypeError("unsupported synthetic evidence value")
    return json.dumps(value, default=decimal_only, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_fingerprint(root):
    """Hash source content, including dirty changes; never settings or journals."""
    root = Path(root)
    paths = [root / name for name in ("run_live_test.ps1", "run_volume_market_maker.py",
                                     "lighter_preflight.py")]
    paths.extend((root / "core").rglob("*.py"))
    paths.extend((root / "tests").glob("mm_v2_wire*.py"))
    paths.extend(root / "tests" / name for name in ("test_mm_v2_wire_session.py",
                                                    "test_mm_v2_launcher.py",
                                                    "mm_v2_windows_process.py"))
    files = {path.relative_to(root).as_posix(): _hash(path)
             for path in sorted(set(paths)) if path.is_file()}
    return {"scope": "entrypoints_core_python_and_wire_tests", "files": files,
            "sha256": hashlib.sha256(_canonical(files).encode()).hexdigest()}


def sdk_fingerprint():
    """Read installed package metadata/binary hashes without loading native code."""
    try:
        distribution = metadata.distribution("lighter-sdk")
    except metadata.PackageNotFoundError:
        return {"status": "unavailable"}
    binaries = {}
    sdk_sources = {}
    for entry in distribution.files or ():
        relative = Path(str(entry))
        if relative.parts[:1] != ("lighter",):
            continue
        path = Path(distribution.locate_file(entry))
        if not path.is_file():
            continue
        if relative.suffix.lower() in {".dll", ".so", ".dylib"}:
            binaries[relative.as_posix()] = _hash(path)
        elif relative.suffix == ".py":
            sdk_sources[relative.as_posix()] = _hash(path)
    return {"status": "available", "distribution": distribution.metadata["Name"],
            "version": distribution.version,
            "python_source_sha256": hashlib.sha256(_canonical(sdk_sources).encode()).hexdigest(),
            "native_files": binaries, "native_executed": False}


def write_evidence(path, *, root, config, scenario, time_mode, venue, exit_code,
                   seed=0, request_counts=None, process=None, source_at_start=None,
                   loaded_code=None):
    """The config argument is the public typed strategy, never account settings."""
    from core.services.market_maker_v2.config import MarketMakerV2Config

    if type(config) is not MarketMakerV2Config:
        raise TypeError("public typed V2 strategy required")
    if scenario not in {"normal", "late_cancel_fill", "lost_cancel_response", "lost_create_response",
                        "unresolved_cancel_response", "placeholder_cancel", "startup_failure",
                        "book_wait_timeout", "book_invalid_nonce", "book_transport_close",
                        "book_invalid_nonce_fill", "book_alignment_recovers", "book_alignment_no_retry"}:
        raise ValueError("unknown synthetic scenario")
    if time_mode not in {"virtual", "real"}:
        raise ValueError("explicit clock mode required")
    if type(exit_code) is not int:
        raise TypeError("completed process or CLI exit code required")
    # Deliberately omit raw responses, credentials, signer objects and account identity.
    final_venue = {key: venue[key] for key in ("position", "cash", "open_order_ids")}
    effective = asdict(config)
    current_source = source_fingerprint(root)
    if source_at_start is not None and source_at_start != current_source:
        raise ValueError("test source changed during run; evidence is not reproducible")
    manifest = {
        "schema": "mm_v2_synthetic_wire_evidence_v1",
        "synthetic": True, "live_economics_evidence": False,
        "scenario": scenario, "seed": seed, "time_mode": time_mode,
        "source": current_source,
        "source_capture": "before_and_after_run" if source_at_start is not None else "run_end_only",
        "loaded_code": loaded_code or [],
        "python": {"executable": sys.executable, "version": platform.python_version()},
        "sdk": sdk_fingerprint(),
        "effective_config": effective,
        "effective_config_sha256": hashlib.sha256(_canonical(effective).encode()).hexdigest(),
        "isolation": {"settings": "synthetic_only", "native_signer": "replaced",
                      "http_ws_transport": "replaced", "os_network_isolation_verified": False},
        "not_covered": ["native_abi", "physical_http_ws_transport", "exchange_matching_engine"],
        "venue_final": final_venue, "exit_code": exit_code,
        "request_counts": request_counts or {}, "process": process,
    }
    Path(path).write_text(_canonical(manifest) + "\n", encoding="utf-8")
    return manifest
