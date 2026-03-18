from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from prod.cloud_run_smoke_check import run_smoke


def _run_cmd(args: list[str]) -> str:
    try:
        completed = subprocess.run(args, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        if args and args[0] == "gcloud":
            raise RuntimeError("gcloud is required on PATH for the Cloud Run rollback drill") from exc
        raise
    return completed.stdout


def _service_json(service: str, region: str) -> dict[str, Any]:
    payload = _run_cmd([
        "gcloud",
        "run",
        "services",
        "describe",
        service,
        "--region",
        region,
        "--format=json",
    ])
    return json.loads(payload)


def _service_url(payload: dict[str, Any]) -> str:
    return str(((payload.get("status") or {}).get("url") or "")).strip()


def _revision_names(payload: Any) -> list[str]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items") or []
    else:
        items = []
    names: list[str] = []
    for item in items:
        name = str(((item.get("metadata") or {}).get("name") or "")).strip()
        if name:
            names.append(name)
    return names


def _current_live_revision(payload: dict[str, Any]) -> str:
    traffic = (payload.get("status") or {}).get("traffic") or []
    for item in traffic:
        if int(item.get("percent", 0) or 0) == 100:
            return str(item.get("revisionName") or "").strip()
    return str(((payload.get("status") or {}).get("latestReadyRevisionName") or "")).strip()


def _list_revisions(service: str, region: str) -> list[str]:
    payload = _run_cmd([
        "gcloud",
        "run",
        "revisions",
        "list",
        "--service",
        service,
        "--region",
        region,
        "--format=json",
    ])
    return _revision_names(json.loads(payload))


def _select_prior_revision(service: str, region: str, current_live_revision: str) -> str:
    revisions = _list_revisions(service, region)
    for name in revisions:
        if name != current_live_revision:
            return name
    raise ValueError("No prior revision available for rollback drill")


def _switch_traffic(service: str, region: str, revision: str) -> None:
    _run_cmd([
        "gcloud",
        "run",
        "services",
        "update-traffic",
        service,
        "--region",
        region,
        "--to-revisions",
        f"{revision}=100",
    ])


def run_cloud_run_rollback_drill(
    *,
    service: str,
    region: str,
    rollback_revision: str,
    attempts: int = 18,
    sleep_seconds: int = 5,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    before = _service_json(service, region)
    base_url = _service_url(before)
    restore_revision = _current_live_revision(before)
    if not base_url:
        raise ValueError("Service URL is unavailable")
    if not restore_revision:
        raise ValueError("Could not determine current live revision")
    if not rollback_revision:
        rollback_revision = _select_prior_revision(service, region, restore_revision)
    if rollback_revision == restore_revision:
        raise ValueError("rollback_revision must differ from current live revision")

    rollback_smoke: dict[str, Any] | None = None
    restore_smoke: dict[str, Any] | None = None
    current_live = restore_revision
    try:
        _switch_traffic(service, region, rollback_revision)
        current_live = rollback_revision
        time.sleep(3)
        rollback_smoke = run_smoke(
            base_url=base_url,
            attempts=attempts,
            sleep_seconds=sleep_seconds,
            timeout_seconds=timeout_seconds,
        )
    finally:
        if current_live != restore_revision:
            _switch_traffic(service, region, restore_revision)
            time.sleep(3)

    restore_smoke = run_smoke(
        base_url=base_url,
        attempts=attempts,
        sleep_seconds=sleep_seconds,
        timeout_seconds=timeout_seconds,
    )

    after = _service_json(service, region)
    after_live = _current_live_revision(after)

    checks = {
        "rollback_smoke_ok": bool((rollback_smoke or {}).get("status") == "pass"),
        "restore_smoke_ok": bool((restore_smoke or {}).get("status") == "pass"),
        "restored_to_original_revision": after_live == restore_revision,
    }
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "service": service,
        "region": region,
        "base_url": base_url,
        "before_live_revision": restore_revision,
        "rollback_revision": rollback_revision,
        "after_live_revision": after_live,
        "checks": checks,
        "rollback_smoke": rollback_smoke,
        "restore_smoke": restore_smoke,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a Cloud Run rollback drill and restore traffic")
    parser.add_argument("--service", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--rollback-revision", default="")
    parser.add_argument("--attempts", type=int, default=18)
    parser.add_argument("--sleep-seconds", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=int, default=20)
    args = parser.parse_args()

    payload = run_cloud_run_rollback_drill(
        service=args.service,
        region=args.region,
        rollback_revision=args.rollback_revision,
        attempts=int(args.attempts),
        sleep_seconds=int(args.sleep_seconds),
        timeout_seconds=int(args.timeout_seconds),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if payload.get("status") == "pass" else 1)
