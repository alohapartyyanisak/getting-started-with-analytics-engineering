from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass
class ProbeResult:
    path: str
    ok: bool
    status_code: int | None
    elapsed_ms: int
    error: str | None = None
    content_type: str | None = None
    body_snippet: str | None = None


def _normalize_base_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _request(url: str, timeout_seconds: int) -> tuple[int, dict[str, str], str]:
    request = Request(url=url, headers={"User-Agent": "go-live-cloud-run-smoke/1.0"}, method="GET")
    with urlopen(request, timeout=timeout_seconds) as response:
        status_code = int(getattr(response, "status", 0) or 0)
        headers = {str(k): str(v) for k, v in response.headers.items()}
        body = response.read().decode("utf-8", errors="replace")
        return status_code, headers, body


def _probe(base_url: str, path: str, timeout_seconds: int) -> ProbeResult:
    started = time.perf_counter()
    url = f"{base_url}{path}"
    try:
        status_code, headers, body = _request(url, timeout_seconds=timeout_seconds)
        content_type = headers.get("Content-Type") or headers.get("content-type")
        snippet = body[:200]
        return ProbeResult(
            path=path,
            ok=200 <= status_code < 300,
            status_code=status_code,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            content_type=content_type,
            body_snippet=snippet,
        )
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if getattr(exc, "fp", None) else ""
        return ProbeResult(
            path=path,
            ok=False,
            status_code=int(getattr(exc, "code", 0) or 0),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=str(exc),
            body_snippet=body[:200],
        )
    except URLError as exc:
        return ProbeResult(
            path=path,
            ok=False,
            status_code=None,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=str(exc.reason),
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        return ProbeResult(
            path=path,
            ok=False,
            status_code=None,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=str(exc),
        )


def run_smoke(
    *,
    base_url: str,
    attempts: int = 12,
    sleep_seconds: int = 5,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    normalized_url = _normalize_base_url(base_url)
    if not normalized_url:
        raise ValueError("base_url is required")

    last_results: list[ProbeResult] = []
    for attempt in range(1, max(1, int(attempts)) + 1):
        results = [
            _probe(normalized_url, "/", timeout_seconds=timeout_seconds),
            _probe(normalized_url, "/_stcore/health", timeout_seconds=timeout_seconds),
            _probe(normalized_url, "/_stcore/host-config", timeout_seconds=timeout_seconds),
        ]
        last_results = results

        root_ok = results[0].ok and bool(results[0].body_snippet)
        health_ok = results[1].ok
        host_config_ok = results[2].ok
        if root_ok and health_ok and host_config_ok:
            return {
                "status": "pass",
                "base_url": normalized_url,
                "attempt": attempt,
                "checks": {
                    "root_ok": root_ok,
                    "streamlit_health_ok": health_ok,
                    "streamlit_host_config_ok": host_config_ok,
                },
                "probes": [result.__dict__ for result in results],
            }

        if attempt < attempts:
            time.sleep(max(0, int(sleep_seconds)))

    return {
        "status": "fail",
        "base_url": normalized_url,
        "attempt": int(attempts),
        "checks": {
            "root_ok": bool(last_results and last_results[0].ok and last_results[0].body_snippet),
            "streamlit_health_ok": bool(len(last_results) > 1 and last_results[1].ok),
            "streamlit_host_config_ok": bool(len(last_results) > 2 and last_results[2].ok),
        },
        "probes": [result.__dict__ for result in last_results],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke-check the deployed go-live Cloud Run app")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--sleep-seconds", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=int, default=15)
    args = parser.parse_args()

    payload = run_smoke(
        base_url=args.base_url,
        attempts=int(args.attempts),
        sleep_seconds=int(args.sleep_seconds),
        timeout_seconds=int(args.timeout_seconds),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if payload.get("status") == "pass" else 1)
