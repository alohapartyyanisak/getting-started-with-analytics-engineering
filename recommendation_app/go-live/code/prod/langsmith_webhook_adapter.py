from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_LANGSMITH_API_URL = "https://api.smith.langchain.com"
DEFAULT_PROJECT = "go-live-ops"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    value = dt or _utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _env_bool(name: str, default: bool = False) -> bool:
    text = str(os.getenv(name, "") or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def _status_is_error(status: object) -> bool:
    return str(status or "").strip().lower() not in {"ok", "noop", "no_changes", "dry_run"}


def _duration_bounds_from_stages(payload: dict[str, Any]) -> tuple[str, str]:
    stages = payload.get("stages")
    if isinstance(stages, list) and stages:
        starts = [
            parsed
            for parsed in (_parse_iso(stage.get("started_at_utc")) for stage in stages if isinstance(stage, dict))
            if parsed is not None
        ]
        ends = [
            parsed
            for parsed in (_parse_iso(stage.get("ended_at_utc")) for stage in stages if isinstance(stage, dict))
            if parsed is not None
        ]
        if starts and ends:
            return _iso(min(starts)), _iso(max(ends))
    now = _utc_now()
    return _iso(now), _iso(now)


def _langsmith_headers() -> dict[str, str]:
    api_key = str(os.getenv("LANGSMITH_API_KEY", "") or "").strip()
    if not api_key:
        raise ValueError("LANGSMITH_API_KEY is required")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "User-Agent": "go-live-langsmith-adapter/1.0",
    }
    workspace_id = str(os.getenv("LANGSMITH_WORKSPACE_ID", "") or "").strip()
    if workspace_id:
        headers["x-tenant-id"] = workspace_id
    return headers


def _langsmith_api_url(path: str) -> str:
    base = str(os.getenv("LANGSMITH_API_URL", DEFAULT_LANGSMITH_API_URL) or "").strip().rstrip("/")
    if not base:
        base = DEFAULT_LANGSMITH_API_URL
    return f"{base}{path}"


def _request_json(method: str, path: str, payload: dict[str, Any]) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url=_langsmith_api_url(path),
        data=body,
        headers=_langsmith_headers(),
        method=method.upper(),
    )
    with urlopen(request, timeout=15) as response:
        status = int(getattr(response, "status", 0) or 0)
        text = response.read().decode("utf-8", errors="replace")
        return status, text


def _post_run(payload: dict[str, Any]) -> tuple[int, str]:
    return _request_json("POST", "/runs", payload)


def _patch_run(run_id: str, payload: dict[str, Any]) -> tuple[int, str]:
    return _request_json("PATCH", f"/runs/{run_id}", payload)


def _project_name() -> str:
    return str(os.getenv("LANGSMITH_PROJECT", DEFAULT_PROJECT) or "").strip() or DEFAULT_PROJECT


def _trimmed_payload(payload: dict[str, Any], keep_keys: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in keep_keys:
        if key in payload:
            out[key] = payload.get(key)
    return out


def _dotted_order_segment(start_time: datetime, run_id: str) -> str:
    return start_time.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + run_id


def _root_dotted_order(start_time: datetime, run_id: str) -> str:
    return _dotted_order_segment(start_time, run_id)


def _child_dotted_order(parent_dotted_order: str, start_time: datetime, run_id: str) -> str:
    return f"{parent_dotted_order}.{_dotted_order_segment(start_time, run_id)}"


def _scheduler_root_outputs(payload: dict[str, Any]) -> dict[str, Any]:
    pointer_after = payload.get("pointer_after")
    pointer_version = ""
    if isinstance(pointer_after, dict):
        pointer_version = str(pointer_after.get("dataset_version", "") or "").strip()
    elif isinstance(pointer_after, str):
        pointer_version = pointer_after.strip()
    return {
        "status": payload.get("status"),
        "mode": payload.get("mode"),
        "base_dataset_version": payload.get("base_dataset_version"),
        "candidate_dataset_version": payload.get("candidate_dataset_version"),
        "pointer_after_dataset_version": pointer_version,
        "promoted": payload.get("promoted"),
        "pointer_restored": payload.get("pointer_restored"),
        "run_log_path": payload.get("run_log_path"),
    }


def _retention_root_outputs(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "apply": payload.get("apply"),
        "active_dataset_version": payload.get("active_dataset_version"),
        "deleted_versions_count": len(payload.get("deleted_versions", []) or []),
        "dry_run_snapshot_deleted_count": len(payload.get("dry_run_snapshot_deleted", []) or []),
        "run_logs_deleted_count": len(payload.get("run_logs_deleted", []) or []),
    }


def _scheduler_child_runs(
    payload: dict[str, Any],
    root_run_id: str,
    trace_id: str,
    root_dotted_order: str,
) -> list[dict[str, Any]]:
    stages = payload.get("stages")
    if not isinstance(stages, list):
        return []

    child_runs: list[dict[str, Any]] = []
    for index, stage in enumerate(stages, start=1):
        if not isinstance(stage, dict):
            continue
        stage_name = str(stage.get("stage", "") or "").strip() or f"stage_{index}"
        start_time = _parse_iso(stage.get("started_at_utc")) or _utc_now()
        end_time = _parse_iso(stage.get("ended_at_utc")) or start_time
        status = str(stage.get("status", "") or "").strip()
        details = stage.get("details") if isinstance(stage.get("details"), dict) else {}
        error = str(stage.get("error", "") or "").strip()
        child_run_id = str(uuid.uuid4())
        child_runs.append(
            {
                "id": child_run_id,
                "trace_id": trace_id,
                "parent_run_id": root_run_id,
                "dotted_order": _child_dotted_order(root_dotted_order, start_time, child_run_id),
                "name": f"scheduler:{stage_name}",
                "run_type": "tool",
                "session_name": _project_name(),
                "inputs": {"stage": stage_name, "status": status},
                "start_time": _iso(start_time),
                "end_time": _iso(end_time),
                "outputs": details or {"elapsed_ms": stage.get("elapsed_ms")},
                "extra": {
                    "metadata": {
                        "event": "prod_scheduler_stage",
                        "stage": stage_name,
                        "elapsed_ms": stage.get("elapsed_ms"),
                    }
                },
                "error": error or None,
                "tags": ["go-live", "scheduler", "stage"],
            }
        )
    return child_runs


def _langsmith_runs_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    event = str(payload.get("event", "") or "").strip()
    root_run_id = str(uuid.uuid4())
    trace_id = root_run_id

    if event == "prod_scheduler_run_result":
        start_time, end_time = _duration_bounds_from_stages(payload)
        start_dt = _parse_iso(start_time) or _utc_now()
        root_dotted_order = _root_dotted_order(start_dt, root_run_id)
        root_run = {
            "id": root_run_id,
            "trace_id": trace_id,
            "dotted_order": root_dotted_order,
            "name": "go-live scheduler",
            "run_type": "chain",
            "session_name": _project_name(),
            "inputs": payload,
            "start_time": start_time,
            "end_time": end_time,
            "outputs": _scheduler_root_outputs(payload),
            "extra": {
                "metadata": {
                    "event": event,
                    "run_id": payload.get("run_id"),
                    "mode": payload.get("mode"),
                    "status_group": payload.get("status_group"),
                    "post_run_smoke_status": (payload.get("post_run_smoke") or {}).get("status")
                    if isinstance(payload.get("post_run_smoke"), dict)
                    else None,
                }
            },
            "error": payload.get("error") if _status_is_error(payload.get("status")) else None,
            "tags": ["go-live", "scheduler", str(payload.get("mode", "") or "").strip() or "unknown"],
        }
        return [
            root_run,
            *_scheduler_child_runs(
                payload,
                root_run_id=root_run_id,
                trace_id=trace_id,
                root_dotted_order=root_dotted_order,
            ),
        ]

    if event == "prod_retention_run_result":
        now = _utc_now()
        root_dotted_order = _root_dotted_order(now, root_run_id)
        root_run = {
            "id": root_run_id,
            "trace_id": trace_id,
            "dotted_order": root_dotted_order,
            "name": "go-live retention",
            "run_type": "chain",
            "session_name": _project_name(),
            "inputs": payload,
            "start_time": _iso(now),
            "end_time": _iso(now),
            "outputs": _retention_root_outputs(payload),
            "extra": {
                "metadata": {
                    "event": event,
                    "status_group": payload.get("status_group"),
                    "apply": payload.get("apply"),
                }
            },
            "error": payload.get("error") if _status_is_error(payload.get("status")) else None,
            "tags": ["go-live", "retention"],
        }
        return [root_run]

    raise ValueError(f"Unsupported event: {event}")


def ingest_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    runs = _langsmith_runs_from_payload(payload)
    posted: list[dict[str, Any]] = []
    patched: list[dict[str, Any]] = []

    for run in runs:
        run_id = str(run["id"])
        post_payload = {
            "id": run_id,
            "trace_id": run.get("trace_id"),
            "parent_run_id": run.get("parent_run_id"),
            "dotted_order": run.get("dotted_order"),
            "name": run["name"],
            "run_type": run["run_type"],
            "session_name": run["session_name"],
            "inputs": run["inputs"],
            "start_time": run["start_time"],
            "extra": run.get("extra", {}),
            "tags": run.get("tags", []),
        }
        post_status, _ = _post_run(post_payload)
        posted.append({"id": run_id, "status": post_status, "name": run["name"]})

        patch_payload = {
            "end_time": run["end_time"],
            "outputs": run.get("outputs", {}),
        }
        error = run.get("error")
        if error:
            patch_payload["error"] = error
        patch_status, _ = _patch_run(run_id, patch_payload)
        patched.append({"id": run_id, "status": patch_status, "name": run["name"]})

    return {
        "ok": True,
        "project": _project_name(),
        "runs_posted": len(posted),
        "runs_patched": len(patched),
        "posted": posted,
        "patched": patched,
    }


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    try:
        content_length = int(handler.headers.get("Content-Length", "0") or "0")
    except Exception:
        content_length = 0
    raw = handler.rfile.read(max(0, content_length))
    if not raw:
        raise ValueError("Request body is empty")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


class LangSmithWebhookHandler(BaseHTTPRequestHandler):
    server_version = "GoLiveLangSmithAdapter/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        expected = str(os.getenv("LANGSMITH_ADAPTER_BEARER_TOKEN", "") or "").strip()
        if not expected:
            return True
        auth = str(self.headers.get("Authorization", "") or "").strip()
        return auth == f"Bearer {expected}"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._write_json(
                200,
                {
                    "ok": True,
                    "service": "go-live-langsmith-adapter",
                    "project": _project_name(),
                    "requires_bearer": bool(str(os.getenv("LANGSMITH_ADAPTER_BEARER_TOKEN", "") or "").strip()),
                },
            )
            return
        self._write_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        if self.path != "/langsmith-ingest":
            self._write_json(404, {"ok": False, "error": "Not found"})
            return
        if not self._check_auth():
            self._write_json(401, {"ok": False, "error": "Unauthorized"})
            return
        try:
            payload = _read_json_body(self)
            result = ingest_webhook_payload(payload)
            self._write_json(200, result)
        except ValueError as exc:
            self._write_json(400, {"ok": False, "error": str(exc)})
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            self._write_json(
                502,
                {
                    "ok": False,
                    "error": f"LangSmith HTTPError: {exc}",
                    "response_body": message,
                },
            )
        except URLError as exc:
            self._write_json(502, {"ok": False, "error": f"LangSmith URLError: {exc}"})
        except Exception as exc:
            self._write_json(500, {"ok": False, "error": str(exc), "error_type": exc.__class__.__name__})


def main() -> int:
    host = str(os.getenv("LANGSMITH_ADAPTER_HOST", DEFAULT_HOST) or "").strip() or DEFAULT_HOST
    port_text = str(os.getenv("PORT", os.getenv("LANGSMITH_ADAPTER_PORT", str(DEFAULT_PORT))) or "").strip()
    try:
        port = int(port_text)
    except Exception:
        port = DEFAULT_PORT

    server = ThreadingHTTPServer((host, port), LangSmithWebhookHandler)
    print(
        json.dumps(
            {
                "ok": True,
                "service": "go-live-langsmith-adapter",
                "host": host,
                "port": port,
                "project": _project_name(),
                "path": "/langsmith-ingest",
                "health_path": "/health",
            },
            ensure_ascii=False,
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
