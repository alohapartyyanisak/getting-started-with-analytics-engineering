from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse


DEFAULT_LAUNCH_STAGE = "soft_launch"
DEFAULT_REDIRECT_STATUS = 303

ALLOWED_HOSTS = {
    "spotify": {"open.spotify.com"},
    "youtube": {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"},
    "temp_playlist": {"youtube.com", "www.youtube.com"},
}

app = FastAPI(title="DJ Mixing Station Redirector", version="0.1.0")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_env(name: str, default: str = "") -> str:
    value = str(os.getenv(name, "") or "").strip()
    return value or default


def _metadata() -> dict[str, Any]:
    return {
        "service": _text_env("K_SERVICE", "dj-mixing-station-redirector"),
        "revision": _text_env("K_REVISION") or None,
        "configuration": _text_env("K_CONFIGURATION") or None,
        "environment": _text_env("GO_LIVE_TRACE_ENVIRONMENT") or None,
    }


def _emit_product_event(
    event_name: str,
    *,
    anonymous_browser_id: str,
    session_id: str,
    request_id: str,
    dataset_snapshot_id: str,
    traffic_source: str,
    launch_stage: str,
    page: str,
    lane: str,
    app_release_version: str,
    event_props: dict[str, Any],
) -> None:
    record = {
        "event_type": "product_analytics",
        "event_name": event_name,
        "event_ts": _utc_now_iso(),
        "event_ts_utc": _utc_now_iso(),
        "anonymous_browser_id": anonymous_browser_id or "unknown_browser",
        "session_id": session_id or "unknown_session",
        "request_id": request_id or "unknown_request",
        "app_release_version": app_release_version or _text_env("GO_LIVE_APP_RELEASE_VERSION", _text_env("K_REVISION", "unknown_release")),
        "dataset_snapshot_id": dataset_snapshot_id or "unknown_dataset",
        "launch_stage": launch_stage or _text_env("GO_LIVE_LAUNCH_STAGE", DEFAULT_LAUNCH_STAGE),
        "page": page or "studio_home",
        "traffic_source": traffic_source or "external_public",
        "lane": lane or "lighter",
        "severity": "INFO",
        "metadata": _metadata(),
        "event_props": event_props,
    }
    sys.stdout.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def _validate_target(target_url: str, endpoint: str) -> str:
    target = str(target_url or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="target_url is required")

    parsed = urlparse(target)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="target_url must use https")

    host = (parsed.netloc or "").lower()
    if host not in ALLOWED_HOSTS[endpoint]:
        raise HTTPException(status_code=403, detail=f"target host not allowed for {endpoint}")
    return target


def _common_context(
    *,
    anonymous_browser_id: str,
    session_id: str,
    request_id: str,
    dataset_snapshot_id: str,
    traffic_source: str,
    launch_stage: str,
    page: str,
    lane: str,
    app_release_version: str,
) -> dict[str, str]:
    return {
        "anonymous_browser_id": anonymous_browser_id,
        "session_id": session_id,
        "request_id": request_id,
        "dataset_snapshot_id": dataset_snapshot_id,
        "traffic_source": traffic_source,
        "launch_stage": launch_stage,
        "page": page,
        "lane": lane,
        "app_release_version": app_release_version,
    }


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": _metadata()["service"]})


@app.get("/open/spotify")
def open_spotify(
    target_url: str = Query(...),
    anonymous_browser_id: str = Query(...),
    session_id: str = Query(...),
    request_id: str = Query(...),
    dataset_snapshot_id: str = Query(...),
    track_name: str = Query(""),
    artist_name: str = Query(""),
    playlist_position: int = Query(0),
    traffic_source: str = Query("external_public"),
    launch_stage: str = Query(DEFAULT_LAUNCH_STAGE),
    page: str = Query("studio_home"),
    lane: str = Query("lighter"),
    app_release_version: str = Query(""),
) -> RedirectResponse:
    target = _validate_target(target_url, "spotify")
    common = _common_context(
        anonymous_browser_id=anonymous_browser_id,
        session_id=session_id,
        request_id=request_id,
        dataset_snapshot_id=dataset_snapshot_id,
        traffic_source=traffic_source,
        launch_stage=launch_stage,
        page=page,
        lane=lane,
        app_release_version=app_release_version,
    )
    _emit_product_event(
        "track_play_clicked",
        **common,
        event_props={
            "platform": "spotify",
            "track_name": track_name,
            "artist_name": artist_name,
            "playlist_position": playlist_position,
        },
    )
    _emit_product_event(
        "spotify_opened",
        **common,
        event_props={
            "track_name": track_name,
            "playlist_position": playlist_position,
            "target_url": target,
        },
    )
    return RedirectResponse(target, status_code=DEFAULT_REDIRECT_STATUS)


@app.get("/open/youtube")
def open_youtube(
    target_url: str = Query(...),
    anonymous_browser_id: str = Query(...),
    session_id: str = Query(...),
    request_id: str = Query(...),
    dataset_snapshot_id: str = Query(...),
    track_name: str = Query(""),
    artist_name: str = Query(""),
    playlist_position: int = Query(0),
    traffic_source: str = Query("external_public"),
    launch_stage: str = Query(DEFAULT_LAUNCH_STAGE),
    page: str = Query("studio_home"),
    lane: str = Query("lighter"),
    app_release_version: str = Query(""),
) -> RedirectResponse:
    target = _validate_target(target_url, "youtube")
    common = _common_context(
        anonymous_browser_id=anonymous_browser_id,
        session_id=session_id,
        request_id=request_id,
        dataset_snapshot_id=dataset_snapshot_id,
        traffic_source=traffic_source,
        launch_stage=launch_stage,
        page=page,
        lane=lane,
        app_release_version=app_release_version,
    )
    _emit_product_event(
        "track_play_clicked",
        **common,
        event_props={
            "platform": "youtube",
            "track_name": track_name,
            "artist_name": artist_name,
            "playlist_position": playlist_position,
        },
    )
    _emit_product_event(
        "youtube_opened",
        **common,
        event_props={
            "track_name": track_name,
            "playlist_position": playlist_position,
            "target_url": target,
        },
    )
    return RedirectResponse(target, status_code=DEFAULT_REDIRECT_STATUS)


@app.get("/open/temp-playlist")
def open_temp_playlist(
    target_url: str = Query(...),
    anonymous_browser_id: str = Query(...),
    session_id: str = Query(...),
    request_id: str = Query(...),
    dataset_snapshot_id: str = Query(...),
    playlist_track_count: int = Query(0),
    coverage_ratio: float = Query(0.0),
    traffic_source: str = Query("external_public"),
    launch_stage: str = Query(DEFAULT_LAUNCH_STAGE),
    page: str = Query("studio_home"),
    lane: str = Query("lighter"),
    app_release_version: str = Query(""),
) -> RedirectResponse:
    target = _validate_target(target_url, "temp_playlist")
    common = _common_context(
        anonymous_browser_id=anonymous_browser_id,
        session_id=session_id,
        request_id=request_id,
        dataset_snapshot_id=dataset_snapshot_id,
        traffic_source=traffic_source,
        launch_stage=launch_stage,
        page=page,
        lane=lane,
        app_release_version=app_release_version,
    )
    _emit_product_event(
        "temp_playlist_opened",
        **common,
        event_props={
            "playlist_track_count": playlist_track_count,
            "coverage_ratio": coverage_ratio,
            "target_url": target,
        },
    )
    return RedirectResponse(target, status_code=DEFAULT_REDIRECT_STATUS)
