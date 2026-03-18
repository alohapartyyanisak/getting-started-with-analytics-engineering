from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from prod.cloud_run_rollback_drill import run_cloud_run_rollback_drill
from prod.dataset_pointer_rollback_drill import run_dataset_pointer_rollback_drill


def run_combined_rollback_drill(
    *,
    service: str,
    region: str,
    rollback_revision: str,
    attempts: int,
    sleep_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    dataset_result = run_dataset_pointer_rollback_drill()
    app_result = run_cloud_run_rollback_drill(
        service=service,
        region=region,
        rollback_revision=rollback_revision,
        attempts=attempts,
        sleep_seconds=sleep_seconds,
        timeout_seconds=timeout_seconds,
    )
    checks = {
        "dataset_drill_ok": bool(dataset_result.get("status") == "pass"),
        "app_drill_ok": bool(app_result.get("status") == "pass"),
    }
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "dataset": dataset_result,
        "app": app_result,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the combined go-live rollback drill")
    parser.add_argument("--service", default=str(os.getenv("CLOUD_RUN_SERVICE", "") or "").strip())
    parser.add_argument("--region", default=str(os.getenv("GCP_REGION", "") or "").strip())
    parser.add_argument("--rollback-revision", default=str(os.getenv("ROLLBACK_REVISION", "") or "").strip())
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--sleep-seconds", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=15)
    args = parser.parse_args()

    if not args.service:
        raise SystemExit("--service or CLOUD_RUN_SERVICE is required")
    if not args.region:
        raise SystemExit("--region or GCP_REGION is required")

    payload = run_combined_rollback_drill(
        service=args.service,
        region=args.region,
        rollback_revision=args.rollback_revision,
        attempts=int(args.attempts),
        sleep_seconds=int(args.sleep_seconds),
        timeout_seconds=int(args.timeout_seconds),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if payload.get("status") == "pass" else 1)
