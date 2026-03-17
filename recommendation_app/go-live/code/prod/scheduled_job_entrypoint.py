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

from scheduler_orchestrator import (  # type: ignore
    DEFAULT_PUBLISH_TIMEOUT_SECONDS,
    orchestrate_scheduler,
)


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


def _status_group(status: str) -> str:
    return "ok" if str(status or "").strip() in {"ok", "noop", "no_changes", "dry_run"} else "error"


def _notify_hook(payload: dict[str, object]) -> dict[str, object]:
    url = str(os.getenv("PROD_SCHED_NOTIFY_URL", "") or "").strip()
    if not url:
        return {"enabled": False, "sent": False, "reason": "notify_url_not_set"}

    notify_on_raw = str(os.getenv("PROD_SCHED_NOTIFY_ON", "ok,error") or "").strip().lower()
    notify_on = {item.strip() for item in notify_on_raw.split(",") if item.strip()}
    if not notify_on:
        notify_on = {"ok", "error"}

    status = str(payload.get("status", "") or "")
    group = _status_group(status)
    if group not in notify_on:
        return {
            "enabled": True,
            "sent": False,
            "reason": "status_filtered",
            "status_group": group,
        }

    timeout_seconds = max(1, _as_int(os.getenv("PROD_SCHED_NOTIFY_TIMEOUT_SECONDS", "10"), 10))
    event = {
        "event": "prod_scheduler_run_result",
        "status": status,
        "status_group": group,
        "run_id": payload.get("run_id"),
        "mode": payload.get("mode"),
        "base_dataset_version": payload.get("base_dataset_version"),
        "candidate_dataset_version": payload.get("candidate_dataset_version"),
        "promoted": payload.get("promoted"),
        "pointer_after": (payload.get("pointer_after") or {}).get("dataset_version")
        if isinstance(payload.get("pointer_after"), dict)
        else None,
        "error": payload.get("error"),
        "run_log_path": payload.get("run_log_path"),
    }
    body = json.dumps(event, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "go-live-prod-scheduler/1.0",
    }
    bearer = str(os.getenv("PROD_SCHED_NOTIFY_BEARER_TOKEN", "") or "").strip()
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
    mode = str(os.getenv("PROD_SCHED_MODE", "problem_queue_bounded")).strip()
    workers = _as_int(os.getenv("PROD_SCHED_WORKERS", "16"), 16)
    force = _as_bool(os.getenv("PROD_SCHED_FORCE", "false"), False)
    behavioral_status = str(os.getenv("PROD_SCHED_BEHAVIORAL_STATUS", "unknown")).strip()
    manual_status = str(os.getenv("PROD_SCHED_MANUAL_STATUS", "unknown")).strip()
    auto_promote = _as_bool(os.getenv("PROD_SCHED_AUTO_PROMOTE", "false"), False)
    publish_timeout_seconds = _as_int(
        os.getenv("PROD_SCHED_PUBLISH_TIMEOUT_SECONDS", str(DEFAULT_PUBLISH_TIMEOUT_SECONDS)),
        DEFAULT_PUBLISH_TIMEOUT_SECONDS,
    )
    problem_max_rows = _as_int(os.getenv("PROD_SCHED_PROBLEM_MAX_ROWS", "0"), 0)
    problem_max_resolve_keys = _as_int(os.getenv("PROD_SCHED_PROBLEM_MAX_RESOLVE_KEYS", "0"), 0)
    run_post_smoke = _as_bool(os.getenv("PROD_SCHED_POST_SMOKE", "true"), True)

    payload = orchestrate_scheduler(
        mode=mode,
        workers=workers,
        force=force,
        behavioral_status=behavioral_status,
        manual_status=manual_status,
        auto_promote=auto_promote,
        publish_timeout_seconds=publish_timeout_seconds,
        problem_max_rows=problem_max_rows,
        problem_max_resolve_keys=problem_max_resolve_keys,
        run_post_smoke=run_post_smoke,
    )
    payload["notification"] = _notify_hook(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if _status_group(str(payload.get("status", ""))) == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
