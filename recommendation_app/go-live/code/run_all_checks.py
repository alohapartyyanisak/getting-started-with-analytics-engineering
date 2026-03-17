from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def _run(cmd: list[str]) -> dict[str, object]:
    started = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "cmd": " ".join(cmd),
        "returncode": int(proc.returncode),
        "elapsed_ms": elapsed_ms,
        "stdout_tail": "\n".join(proc.stdout.splitlines()[-40:]),
        "stderr_tail": "\n".join(proc.stderr.splitlines()[-40:]),
    }


def main() -> int:
    repo_root = Path(__file__).resolve().parents[3]
    checks: list[list[str]] = [
        [sys.executable, "-m", "py_compile", "recommendation_app/offline-v2/code/app.py", "recommendation_app/offline-v2/code/application_v2.py"],
        [sys.executable, "-m", "py_compile", *sorted(str(p) for p in (repo_root / "recommendation_app/go-live/code").glob("*.py"))],
        [sys.executable, "recommendation_app/go-live/code/phase1_health_check.py"],
        [sys.executable, "recommendation_app/go-live/code/phase1_regression_benchmark.py"],
        [sys.executable, "recommendation_app/go-live/code/phase1_playlist_order_regression.py"],
        [sys.executable, "recommendation_app/go-live/code/phase1_latency_benchmark.py", "--iterations", "25", "--p95-threshold-ms", "2500"],
        [sys.executable, "recommendation_app/go-live/code/phase1_duplicate_link_validation.py"],
        [sys.executable, "recommendation_app/go-live/code/phase2_health_check.py"],
        [sys.executable, "recommendation_app/go-live/code/phase2_rollback_smoke.py"],
    ]

    results = []
    failed = 0
    for cmd in checks:
        result = _run(cmd)
        if result["returncode"] != 0:
            failed += 1
        results.append(result)

    payload = {
        "status": "pass" if failed == 0 else "fail",
        "failed": failed,
        "checks": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
