"""Run the complete CryptoGuard suite with a proven parallel/serial split."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


TEST_ROOT = Path(__file__).resolve().parent


def _node_ids(output: str) -> set[str]:
    return {
        line.strip()
        for line in output.splitlines()
        if "::" in line and not line.lstrip().startswith(("=", "<"))
    }


def _pytest(
    args: list[str], *, capture: bool = False, node_ids: set[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    args_file: Path | None = None
    try:
        if node_ids is None:
            selection = [str(TEST_ROOT)]
        else:
            # Windows' command-line limit cannot carry 1,300+ node IDs. Pytest
            # argfiles preserve the exact pre-validated set without collecting
            # its complement, so final stages report zero deselections.
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".pytest-args", delete=False,
            ) as handle:
                handle.write("\n".join(sorted(node_ids)))
                handle.write("\n")
                args_file = Path(handle.name)
            selection = [f"@{args_file}"]
        command = [sys.executable, "-m", "pytest", *selection, *args]
        return subprocess.run(
            command,
            cwd=TEST_ROOT.parents[2],
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            text=True,
            capture_output=capture,
            check=False,
        )
    finally:
        if args_file is not None:
            args_file.unlink(missing_ok=True)


_SUMMARY_RE = re.compile(
    r"^\s*=*\s*.*?\b(\d+)\s+passed\b.*?in\s+\d+(?:\.\d+)?s\b"
    r"(?:\s*\(\d+:\d+(?::\d+)?\))?\s*=*\s*$"
)


def _last_summary_line(combined: str) -> str | None:
    """Return the LAST complete pytest summary banner in ``combined``.

    pytest prints its summary banner at process exit, so the last complete
    banner is the authoritative outcome for the captured run. ``-rA`` log text
    may contain ``<number> failed`` / ``<number> skipped`` phrases, but those
    only parse as a complete banner when they are part of a line that also ends
    in ``in <seconds>s``; a lone application-log line never does. Returns
    ``None`` when no complete banner is found (fail-closed: callers must not
    treat the stage as exact).
    """
    banner = None
    for line in combined.splitlines():
        if _SUMMARY_RE.match(line):
            banner = line
    return banner


def _stage_counts(combined: str) -> tuple[int, dict[str, int]]:
    """Parse pytest's authoritative outcome counts from captured stage output.

    ``-rA`` (the change-aware runner's stage flag) appends EVERY test's
    captured output — including application log records — to the report. Log
    text can contain ``<number> failed`` / ``<number> skipped`` phrases
    (fail-closed batch messages like ``..._1783641599999 failed identity
    contract``, ``enabled=10 queued=10 skipped=0``, ``10 failed jobs``, or
    ``3 skipped records``) that an UNANCHORED scan misreads as real test
    outcomes, false-flagging a green stage "not exact".
    终审返工 P2 (08-10): ONLY the LAST complete pytest summary banner is
    parsed — keywords are never accumulated globally across the whole captured
    text. The first ``<digits> passed`` in that banner is the authoritative
    count (handles ``1 failed, 2121 passed`` and ``2120 passed, 10 subtests
    passed`` orderings); each nonzero label is its first match or 0. No banner
    at all → ``(-1, zeros)``, which ``_run_exact_stage`` treats as inexact
    (fail-closed, never a vacuous GREEN).
    """
    banner = _last_summary_line(combined)
    if banner is None:
        return -1, {"failed": 0, "skipped": 0, "deselected": 0}
    first_passed = re.search(r"\b(\d+)\s+passed\b", banner)
    passed = int(first_passed.group(1)) if first_passed else -1
    nonzero = {}
    for label in ("failed", "skipped", "deselected"):
        m = re.search(rf"\b(\d+)\s+{label}\b", banner)
        nonzero[label] = int(m.group(1)) if m else 0
    return passed, nonzero


def _run_exact_stage(
    name: str, node_ids: set[str], args: list[str],
) -> subprocess.CompletedProcess[str]:
    result = _pytest(args, capture=True, node_ids=node_ids)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    combined = f"{result.stdout}\n{result.stderr}"
    passed, nonzero = _stage_counts(combined)
    if result.returncode == 0 and passed != len(node_ids):
        raise RuntimeError(
            f"{name} stage passed-count mismatch: selected={len(node_ids)} "
            f"reported={passed}"
        )
    if result.returncode == 0 and any(nonzero.values()):
        raise RuntimeError(f"{name} stage was not exact: {nonzero}")
    if result.returncode == 0:
        print(
            f"stage_ok name={name} selected={len(node_ids)} passed={passed} "
            "failed=0 skipped=0 deselected=0"
        )
    return result


def _collect(marker: str | None = None) -> set[str]:
    args = ["--collect-only", "-q"]
    if marker is not None:
        args.extend(["-m", marker])
    result = _pytest(args, capture=True)
    if result.returncode not in (0, 5):
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return _node_ids(result.stdout)


def verify_partition() -> tuple[set[str], set[str], set[str]]:
    all_nodes = _collect()
    parallel_nodes = _collect("not serial")
    serial_nodes = _collect("serial")
    overlap = parallel_nodes & serial_nodes
    missing = all_nodes - (parallel_nodes | serial_nodes)
    unexpected = (parallel_nodes | serial_nodes) - all_nodes
    if not all_nodes or overlap or missing or unexpected:
        raise RuntimeError(
            "invalid test partition: "
            f"all={len(all_nodes)} parallel={len(parallel_nodes)} "
            f"serial={len(serial_nodes)} overlap={len(overlap)} "
            f"missing={len(missing)} unexpected={len(unexpected)}"
        )
    print(
        "partition_ok "
        f"all={len(all_nodes)} parallel={len(parallel_nodes)} "
        f"serial={len(serial_nodes)}"
    )
    return all_nodes, parallel_nodes, serial_nodes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 4))
    parser.add_argument("--durations", type=int, default=50)
    parser.add_argument("--verify-partition-only", action="store_true")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if not (
        os.environ.get("CRYPTO_GUARD_DB_ADMIN_PASSWORD")
        or os.environ.get("CRYPTO_GUARD_DATABASE_URL")
    ):
        parser.error(
            "set CRYPTO_GUARD_DB_ADMIN_PASSWORD or CRYPTO_GUARD_DATABASE_URL "
            "for the dedicated crypto_guard_test database"
        )

    _all, parallel, serial = verify_partition()
    if args.verify_partition_only:
        return 0

    started = time.perf_counter()
    parallel_result = _run_exact_stage(
        "parallel",
        parallel,
        [
            "-q",
            "-p",
            "xdist.plugin",
            "-n",
            str(args.workers),
            "--dist",
            "worksteal",
            f"--durations={args.durations}",
        ],
    )
    if parallel_result.returncode != 0:
        return parallel_result.returncode

    if serial:
        serial_result = _run_exact_stage(
            "serial", serial, ["-q", f"--durations={args.durations}"]
        )
        if serial_result.returncode != 0:
            return serial_result.returncode
    print(f"complete_suite_ok elapsed_seconds={time.perf_counter() - started:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
