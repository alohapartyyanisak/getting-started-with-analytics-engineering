from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any


DEFAULT_SCORING_CONFIG_VERSION = "score_cfg_v1"
DEFAULT_RESOLVER_VERSION = "resolver_v1"
DEFAULT_LAUNCH_STAGE = "soft_launch"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_env(name: str, default: str = "") -> str:
    value = str(os.getenv(name, "") or "").strip()
    return value or default


def _service_metadata() -> dict[str, Any]:
    return {
        "service": _text_env("K_SERVICE", "go-live-app"),
        "revision": _text_env("K_REVISION") or None,
        "configuration": _text_env("K_CONFIGURATION") or None,
        "environment": _text_env("GO_LIVE_TRACE_ENVIRONMENT") or None,
    }


def _envelope(
    *,
    event_name: str,
    request_id: str,
    session_id: str,
    dataset_snapshot_id: str,
    severity: str,
) -> dict[str, Any]:
    return {
        "event_type": "operational",
        "event_name": event_name,
        "event_ts": _utc_now_iso(),
        "request_id": request_id or "unknown_request",
        "session_id": session_id or "unknown_session",
        "app_release_version": _text_env("GO_LIVE_APP_RELEASE_VERSION", _text_env("K_REVISION", "unknown_release")),
        "dataset_snapshot_id": dataset_snapshot_id or "unknown_dataset",
        "scoring_config_version": _text_env("GO_LIVE_SCORING_CONFIG_VERSION", DEFAULT_SCORING_CONFIG_VERSION),
        "resolver_version": _text_env("GO_LIVE_RESOLVER_VERSION", DEFAULT_RESOLVER_VERSION),
        "severity": severity,
        "lane": "lighter",
        "metadata": _service_metadata(),
    }


def _product_envelope(
    *,
    event_name: str,
    anonymous_browser_id: str,
    session_id: str,
    request_id: str,
    dataset_snapshot_id: str,
    page: str,
) -> dict[str, Any]:
    event_ts = _utc_now_iso()
    return {
        "event_type": "product_analytics",
        "event_name": event_name,
        "event_ts": event_ts,
        "event_ts_utc": event_ts,
        "anonymous_browser_id": anonymous_browser_id or "unknown_browser",
        "session_id": session_id or "unknown_session",
        "request_id": request_id or "unknown_request",
        "app_release_version": _text_env("GO_LIVE_APP_RELEASE_VERSION", _text_env("K_REVISION", "unknown_release")),
        "dataset_snapshot_id": dataset_snapshot_id or "unknown_dataset",
        "launch_stage": _text_env("GO_LIVE_LAUNCH_STAGE", DEFAULT_LAUNCH_STAGE),
        "page": page or "studio_home",
        "lane": "lighter",
        "severity": "INFO",
        "metadata": _service_metadata(),
    }


def emit_event(
    event_name: str,
    *,
    request_id: str,
    session_id: str,
    dataset_snapshot_id: str,
    severity: str = "INFO",
    **payload: Any,
) -> None:
    record = _envelope(
        event_name=event_name,
        request_id=request_id,
        session_id=session_id,
        dataset_snapshot_id=dataset_snapshot_id,
        severity=severity,
    )
    record.update(payload)
    try:
        sys.stdout.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        sys.stdout.flush()
    except Exception:
        return


def emit_product_event(
    event_name: str,
    *,
    anonymous_browser_id: str,
    session_id: str,
    request_id: str,
    dataset_snapshot_id: str,
    page: str = "studio_home",
    **event_props: Any,
) -> None:
    record = _product_envelope(
        event_name=event_name,
        anonymous_browser_id=anonymous_browser_id,
        session_id=session_id,
        request_id=request_id,
        dataset_snapshot_id=dataset_snapshot_id,
        page=page,
    )
    record["event_props"] = event_props
    try:
        sys.stdout.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        sys.stdout.flush()
    except Exception:
        return


def emit_request_received(
    *,
    request_id: str,
    session_id: str,
    dataset_snapshot_id: str,
    mode: str,
    start_mode: str | None,
    target_minutes: int,
    tolerance_minutes: int,
    platform_bias_spotify_pct: int,
    discovery_hits_pct: int,
    vibe_option: str | None,
    final_pick_count: int,
) -> None:
    emit_event(
        "request_received",
        request_id=request_id,
        session_id=session_id,
        dataset_snapshot_id=dataset_snapshot_id,
        mode=mode,
        start_mode=start_mode,
        target_minutes=int(target_minutes),
        tolerance_minutes=int(tolerance_minutes),
        platform_bias_spotify_pct=int(platform_bias_spotify_pct),
        discovery_hits_pct=int(discovery_hits_pct),
        vibe_option=vibe_option,
        final_pick_count=int(final_pick_count),
    )


def emit_seed_resolution_completed(
    *,
    request_id: str,
    session_id: str,
    dataset_snapshot_id: str,
    selected_artists_count: int,
    selected_songs_count: int,
    seed_display_names: list[str],
    seed_artist_weights: dict[str, float],
) -> None:
    emit_event(
        "seed_resolution_completed",
        request_id=request_id,
        session_id=session_id,
        dataset_snapshot_id=dataset_snapshot_id,
        selected_artists_count=int(selected_artists_count),
        selected_songs_count=int(selected_songs_count),
        seed_display_names=seed_display_names,
        seed_artist_weights=seed_artist_weights,
    )


def emit_ranking_completed(
    *,
    request_id: str,
    session_id: str,
    dataset_snapshot_id: str,
    candidate_pool_size: int,
    ranked_count: int,
    top_display_names: list[str],
    latency_ms: int,
) -> None:
    emit_event(
        "ranking_completed",
        request_id=request_id,
        session_id=session_id,
        dataset_snapshot_id=dataset_snapshot_id,
        candidate_pool_size=int(candidate_pool_size),
        ranked_count=int(ranked_count),
        top_display_names=top_display_names,
        latency_ms=int(latency_ms),
    )


def emit_playlist_optimized(
    *,
    request_id: str,
    session_id: str,
    dataset_snapshot_id: str,
    playlist_track_count: int,
    playlist_total_seconds: int,
    target_seconds: int,
    window_min_seconds: int,
    window_max_seconds: int,
    in_target_window: bool,
    optimizer_latency_ms: int,
) -> None:
    emit_event(
        "playlist_optimized",
        request_id=request_id,
        session_id=session_id,
        dataset_snapshot_id=dataset_snapshot_id,
        playlist_track_count=int(playlist_track_count),
        playlist_total_seconds=int(playlist_total_seconds),
        target_seconds=int(target_seconds),
        window_min_seconds=int(window_min_seconds),
        window_max_seconds=int(window_max_seconds),
        in_target_window=bool(in_target_window),
        optimizer_latency_ms=int(optimizer_latency_ms),
    )


def emit_response_sent(
    *,
    request_id: str,
    session_id: str,
    dataset_snapshot_id: str,
    playlist_track_count: int,
    latency_ms: int,
    response_status: str = "ok",
) -> None:
    emit_event(
        "response_sent",
        request_id=request_id,
        session_id=session_id,
        dataset_snapshot_id=dataset_snapshot_id,
        playlist_track_count=int(playlist_track_count),
        latency_ms=int(latency_ms),
        response_status=response_status,
    )


def emit_resolver_quality_evaluated(
    *,
    request_id: str,
    session_id: str,
    dataset_snapshot_id: str,
    target_rows: int,
    linked_rows: int,
    playable_rows: int,
    unresolved_rows: int,
    resolver_attempted: int,
    resolver_resolved: int,
    included_direct: int,
    included_resolved: int,
    budget_blocked_rows: int,
    degraded: bool,
    degradation_reason: str,
) -> None:
    coverage_ratio = (float(linked_rows) / float(target_rows)) if target_rows > 0 else 0.0
    unresolved_ratio = (float(unresolved_rows) / float(target_rows)) if target_rows > 0 else 0.0
    resolver_success_ratio = (
        float(resolver_resolved) / float(resolver_attempted) if resolver_attempted > 0 else 0.0
    )
    emit_event(
        "resolver_quality_evaluated",
        request_id=request_id,
        session_id=session_id,
        dataset_snapshot_id=dataset_snapshot_id,
        target_rows=int(target_rows),
        linked_rows=int(linked_rows),
        playable_rows=int(playable_rows),
        unresolved_rows=int(unresolved_rows),
        resolver_attempted=int(resolver_attempted),
        resolver_resolved=int(resolver_resolved),
        included_direct=int(included_direct),
        included_resolved=int(included_resolved),
        budget_blocked_rows=int(budget_blocked_rows),
        coverage_ratio=round(coverage_ratio, 4),
        unresolved_ratio=round(unresolved_ratio, 4),
        resolver_success_ratio=round(resolver_success_ratio, 4),
        degraded=bool(degraded),
        degradation_reason=degradation_reason,
    )


def emit_error(
    *,
    request_id: str,
    session_id: str,
    dataset_snapshot_id: str,
    error_type: str,
    error_code: str,
    error_message: str,
    stage: str,
    latency_ms: int,
) -> None:
    emit_event(
        "error",
        request_id=request_id,
        session_id=session_id,
        dataset_snapshot_id=dataset_snapshot_id,
        severity="ERROR",
        error_type=error_type,
        error_code=error_code,
        error_message=error_message,
        stage=stage,
        latency_ms=int(latency_ms),
    )
