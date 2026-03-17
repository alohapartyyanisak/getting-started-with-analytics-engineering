from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from snapshot_retention import run_retention  # type: ignore


def _as_bool(value: str, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def _as_int(value: str, default: int) -> int:
    try:
        return int(str(value or "").strip())
    except Exception:
        return int(default)


def _notify_hook(payload: dict[str, object]) -> dict[str, object]:
    # Retention-specific URL takes priority; scheduler URL is fallback.
    url = str(os.getenv("PROD_RETENTION_NOTIFY_URL", "") or "").strip() or str(
        os.getenv("PROD_SCHED_NOTIFY_URL", "") or ""
    ).strip()
    if not url:
        return {"enabled": False, "sent": False, "reason": "notify_url_not_set"}

    notify_on_raw = str(os.getenv("PROD_RETENTION_NOTIFY_ON", "ok,error") or "").strip().lower()
    notify_on = {item.strip() for item in notify_on_raw.split(",") if item.strip()}
    if not notify_on:
        notify_on = {"ok", "error"}

    status = str(payload.get("status", "") or "")
    group = "ok" if status == "ok" else "error"
    if group not in notify_on:
        return {"enabled": True, "sent": False, "reason": "status_filtered", "status_group": group}

    timeout_seconds = max(1, _as_int(os.getenv("PROD_RETENTION_NOTIFY_TIMEOUT_SECONDS", "10"), 10))
    event = {
        "event": "prod_retention_run_result",
        "status": status,
        "status_group": group,
        "retention_days": payload.get("retention_days"),
        "dry_run_snapshot_retention_days": payload.get("dry_run_snapshot_retention_days"),
        "run_log_retention_days": payload.get("run_log_retention_days"),
        "apply": payload.get("apply"),
        "active_dataset_version": payload.get("active_dataset_version"),
        "deleted_versions_count": len(payload.get("deleted_versions", []) or []),
        "dry_run_snapshot_deleted_count": len(payload.get("dry_run_snapshot_deleted", []) or []),
        "run_logs_deleted_count": len(payload.get("run_logs_deleted", []) or []),
        "error": payload.get("error"),
    }
    body = json.dumps(event, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "go-live-prod-retention/1.0",
    }
    bearer = str(os.getenv("PROD_RETENTION_NOTIFY_BEARER_TOKEN", "") or "").strip() or str(
        os.getenv("PROD_SCHED_NOTIFY_BEARER_TOKEN", "") or ""
    ).strip()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    request = Request(url=url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status_code = int(getattr(response, "status", 0) or 0)
            return {
                "enabled": True,
                "sent": True,
                "status_group": group,
                "http_status": status_code,
                "url": url,
            }
    except URLError as exc:
        return {
            "enabled": True,
            "sent": False,
            "status_group": group,
            "error": str(exc),
            "url": url,
        }
    except Exception as exc:
        return {
            "enabled": True,
            "sent": False,
            "status_group": group,
            "error": str(exc),
            "url": url,
        }


def main() -> int:
    retention_days = _as_int(os.getenv("PROD_RETENTION_DAYS", "60"), 60)
    dry_run_snapshot_retention_days = _as_int(
        os.getenv("PROD_DRY_RUN_SNAPSHOT_RETENTION_DAYS", "14"),
        14,
    )
    run_log_retention_days = _as_int(os.getenv("PROD_RUN_LOG_RETENTION_DAYS", "30"), 30)
    apply = _as_bool(os.getenv("PROD_RETENTION_APPLY", "false"), False)

    try:
        payload = run_retention(
            retention_days=retention_days,
            dry_run_snapshot_retention_days=dry_run_snapshot_retention_days,
            run_log_retention_days=run_log_retention_days,
            apply=apply,
        )
    except Exception as exc:
        payload = {
            "status": "error",
            "retention_days": retention_days,
            "dry_run_snapshot_retention_days": dry_run_snapshot_retention_days,
            "run_log_retention_days": run_log_retention_days,
            "apply": apply,
            "error": str(exc),
            "error_type": exc.__class__.__name__,
        }

    payload["notification"] = _notify_hook(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if str(payload.get("status", "")) == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
