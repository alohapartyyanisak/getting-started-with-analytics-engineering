from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.request import Request, urlopen


DEFAULT_LANGSMITH_API_URL = "https://api.smith.langchain.com"
DEFAULT_PROJECT = "go-live-ops"
_EMITTED_TRACE_KEYS: set[str] = set()


def _env_enabled(name: str, default: bool = False) -> bool:
    text = str(os.getenv(name, "") or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _dotted_order(start_time: datetime, run_id: str) -> str:
    return start_time.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + run_id


def _project_name() -> str:
    return str(os.getenv("LANGSMITH_PROJECT", DEFAULT_PROJECT) or "").strip() or DEFAULT_PROJECT


def _api_url(path: str) -> str:
    base = str(os.getenv("LANGSMITH_API_URL", DEFAULT_LANGSMITH_API_URL) or "").strip().rstrip("/")
    if not base:
        base = DEFAULT_LANGSMITH_API_URL
    return f"{base}{path}"


def _headers() -> dict[str, str]:
    api_key = str(os.getenv("LANGSMITH_API_KEY", "") or "").strip()
    if not api_key:
        raise ValueError("LANGSMITH_API_KEY is required")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "User-Agent": "go-live-app-tracing/1.0",
    }
    workspace_id = str(os.getenv("LANGSMITH_WORKSPACE_ID", "") or "").strip()
    if workspace_id:
        headers["x-tenant-id"] = workspace_id
    return headers


def _request_json(method: str, path: str, payload: dict[str, Any]) -> tuple[int, str]:
    request = Request(
        url=_api_url(path),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_headers(),
        method=method.upper(),
    )
    with urlopen(request, timeout=5) as response:
        return int(getattr(response, "status", 0) or 0), response.read().decode("utf-8", errors="replace")


def _service_metadata() -> dict[str, Any]:
    return {
        "service": str(os.getenv("K_SERVICE", "") or "").strip() or "go-live-app",
        "revision": str(os.getenv("K_REVISION", "") or "").strip() or None,
        "configuration": str(os.getenv("K_CONFIGURATION", "") or "").strip() or None,
        "environment": str(os.getenv("GO_LIVE_TRACE_ENVIRONMENT", "") or "").strip() or None,
    }


def _emit_once(trace_key: str, payload: dict[str, Any]) -> None:
    if trace_key in _EMITTED_TRACE_KEYS:
        return
    if not _env_enabled("GO_LIVE_APP_TRACE_ENABLED", default=False):
        return
    if not str(os.getenv("LANGSMITH_API_KEY", "") or "").strip():
        return
    try:
        _request_json("POST", "/runs", payload)
        _request_json(
            "PATCH",
            f"/runs/{payload['id']}",
            {
                "end_time": payload["end_time"],
                "outputs": payload.get("outputs", {}),
            },
        )
        _EMITTED_TRACE_KEYS.add(trace_key)
    except Exception:
        # Tracing is best-effort and must never block app startup or dataset load.
        return


def emit_app_boot_trace() -> None:
    started_at = _utc_now()
    run_id = str(uuid.uuid4())
    trace_key = "app_boot"
    meta = _service_metadata()
    payload = {
        "id": run_id,
        "trace_id": run_id,
        "dotted_order": _dotted_order(started_at, run_id),
        "name": "go-live app boot",
        "run_type": "chain",
        "session_name": _project_name(),
        "inputs": {
            "port": str(os.getenv("PORT", "") or "").strip() or None,
            "dataset_version_override": str(os.getenv("GO_LIVE_DATASET_VERSION", "") or "").strip() or None,
        },
        "start_time": _iso(started_at),
        "end_time": _iso(_utc_now()),
        "outputs": {
            "status": "ok",
            "storage_root": str(os.getenv("GO_LIVE_DATASET_STORAGE_ROOT", "") or "").strip() or None,
        },
        "extra": {"metadata": {"event": "go_live_app_boot", **meta}},
        "tags": ["go-live", "app", "boot"],
    }
    _emit_once(trace_key, payload)


def emit_dataset_load_trace(*, dataset_version: str, row_count: int, prepared_uri: str) -> None:
    started_at = _utc_now()
    run_id = str(uuid.uuid4())
    trace_key = f"dataset_load::{dataset_version}"
    meta = _service_metadata()
    payload = {
        "id": run_id,
        "trace_id": run_id,
        "dotted_order": _dotted_order(started_at, run_id),
        "name": "go-live dataset load",
        "run_type": "chain",
        "session_name": _project_name(),
        "inputs": {
            "dataset_version": dataset_version,
            "prepared_uri": prepared_uri,
        },
        "start_time": _iso(started_at),
        "end_time": _iso(_utc_now()),
        "outputs": {
            "status": "ok",
            "dataset_version": dataset_version,
            "row_count": int(row_count),
        },
        "extra": {"metadata": {"event": "go_live_dataset_load", **meta}},
        "tags": ["go-live", "app", "dataset-load"],
    }
    _emit_once(trace_key, payload)
