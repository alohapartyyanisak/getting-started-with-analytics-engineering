from __future__ import annotations

import hashlib
import sys
import time
import uuid
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import pandas as pd

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import phase2_runtime_config as cfg
import prod.application_phase2 as phase2_app
from prod.application_phase2 import patch_base_app_for_phase2, base_app, read_source_phase2
from prod.go_live_structured_logging import (
    emit_error,
    emit_playlist_optimized,
    emit_product_event,
    emit_ranking_completed,
    emit_request_received,
    emit_resolver_quality_evaluated,
    emit_response_sent,
    emit_seed_resolution_completed,
)

PUBLIC_UI_CACHE_VERSION = "studio_dev_playlist_v3"
RESOLVER_UNRESOLVED_RATIO_WARN = 0.20
RESOLVER_COVERAGE_RATIO_WARN = 0.80


def _resolver_degradation_reason(yt_stats: dict[str, Any], *, target_rows: int, linked_rows: int) -> str:
    if target_rows <= 0:
        return "no_target_rows"
    unresolved_rows = int(yt_stats.get("unresolved_rows", 0) or 0)
    resolver_attempted = int(yt_stats.get("resolver_attempted", 0) or 0)
    resolver_resolved = int(yt_stats.get("resolver_resolved", 0) or 0)
    budget_blocked_rows = int(yt_stats.get("budget_blocked_rows", 0) or 0)

    coverage_ratio = float(linked_rows) / float(target_rows)
    unresolved_ratio = float(unresolved_rows) / float(target_rows)
    resolver_success_ratio = (
        float(resolver_resolved) / float(resolver_attempted) if resolver_attempted > 0 else 1.0
    )

    if budget_blocked_rows > 0:
        return "resolver_budget_blocked"
    if unresolved_ratio > RESOLVER_UNRESOLVED_RATIO_WARN:
        return "unresolved_ratio_high"
    if coverage_ratio < RESOLVER_COVERAGE_RATIO_WARN:
        return "playlist_coverage_low"
    if resolver_attempted >= 5 and resolver_success_ratio < 0.20:
        return "resolver_success_low"
    return "healthy"


def _render_hidden_startup_health(status: str) -> None:
    base_app.st.markdown(
        (
            '<div data-testid="startup-health-status" '
            'style="display:none;visibility:hidden;height:0;overflow:hidden;">'
            f"{status}"
            "</div>"
        ),
        unsafe_allow_html=True,
    )



def _dataset_snapshot_id(source_path: str) -> str:
    prefix = "managed_snapshot::"
    text = str(source_path or "").strip()
    if text.startswith(prefix):
        version = text[len(prefix) :].strip()
        if version:
            return version
    return str(cfg.DATASET_VERSION or "").strip() or "unknown_dataset"



def _session_id() -> str:
    session_id = str(base_app.st.session_state.get("public_session_id", "") or "").strip()
    if not session_id:
        session_id = str(uuid.uuid4())
        base_app.st.session_state["public_session_id"] = session_id
    return session_id


def _anonymous_browser_id() -> str:
    browser_id = str(base_app.st.session_state.get("public_anonymous_browser_id", "") or "").strip()
    if not browser_id:
        browser_id = str(uuid.uuid4())
        base_app.st.session_state["public_anonymous_browser_id"] = browser_id
    return browser_id


def _product_event_state() -> dict[str, Any]:
    state = base_app.st.session_state.get("public_product_event_state")
    if not isinstance(state, dict):
        state = {}
        base_app.st.session_state["public_product_event_state"] = state
    return state


def _query_param_values(name: str) -> list[str]:
    values: list[str] = []
    try:
        query_params = getattr(base_app.st, "query_params", None)
        if query_params is not None:
            raw = query_params.get(name)
            if isinstance(raw, list):
                values.extend(str(item).strip() for item in raw if str(item).strip())
            elif raw is not None:
                text = str(raw).strip()
                if text:
                    values.append(text)
    except Exception:
        pass

    if not values:
        try:
            ctx = getattr(base_app.st, "context", None)
            request = getattr(ctx, "request", None) if ctx is not None else None
            url = getattr(request, "url", "") if request is not None else ""
            if url:
                parsed = urlparse(str(url))
                query = parse_qs(parsed.query)
                values.extend(str(item).strip() for item in query.get(name, []) if str(item).strip())
        except Exception:
            pass
    return values


def _product_traffic_source() -> str:
    state_key = "public_product_traffic_source"
    cached = str(base_app.st.session_state.get(state_key, "") or "").strip()
    if cached:
        return cached

    traffic_source = ""
    explicit_values = _query_param_values("traffic_source")
    if explicit_values:
        traffic_source = explicit_values[-1].strip().lower()

    if not traffic_source:
        internal_test_values = _query_param_values("internal_test")
        if any(value.strip().lower() in {"1", "true", "yes", "y", "on"} for value in internal_test_values):
            traffic_source = "internal_test"

    if not traffic_source:
        traffic_source = "external_public"

    base_app.st.session_state[state_key] = traffic_source
    return traffic_source


def _pending_tracked_open() -> dict[str, str] | None:
    keys = [
        "open_event",
        "target_url",
        "platform",
        "track_name",
        "artist_name",
        "playlist_position",
        "playlist_track_count",
        "coverage_ratio",
        "analytics_nonce",
        "analytics_browser_id",
        "analytics_session_id",
        "analytics_request_id",
        "traffic_source",
    ]
    values: dict[str, str] = {}
    for key in keys:
        candidates = _query_param_values(key)
        if candidates:
            values[key] = candidates[-1]
    if not values.get("open_event") or not values.get("target_url"):
        return None
    return values


def _tracked_outbound_url(
    *,
    target_url: str,
    open_event: str,
    traffic_source: str,
    anonymous_browser_id: str,
    session_id: str,
    request_id: str,
    platform: str = "",
    track_name: str = "",
    artist_name: str = "",
    playlist_position: int | None = None,
    playlist_track_count: int | None = None,
    coverage_ratio: float | None = None,
) -> str:
    params: dict[str, str] = {
        "open_event": open_event,
        "target_url": target_url,
        "analytics_nonce": str(uuid.uuid4()),
        "analytics_browser_id": anonymous_browser_id,
        "analytics_session_id": session_id,
        "analytics_request_id": request_id or "unknown_request",
        "traffic_source": traffic_source,
    }
    if traffic_source == "internal_test":
        params["internal_test"] = "1"
    if platform:
        params["platform"] = platform
    if track_name:
        params["track_name"] = track_name
    if artist_name:
        params["artist_name"] = artist_name
    if playlist_position is not None:
        params["playlist_position"] = str(int(playlist_position))
    if playlist_track_count is not None:
        params["playlist_track_count"] = str(int(playlist_track_count))
    if coverage_ratio is not None:
        params["coverage_ratio"] = f"{float(coverage_ratio):.4f}"
    return "?" + urlencode(params)


def _render_tracked_platform_link(
    label: str,
    target_url: str,
    *,
    tracked_url: str,
) -> None:
    safe_label = escape(str(label or "").strip())
    safe_href = escape(str(tracked_url or "").strip(), quote=True)
    base_app.st.markdown(
        (
            f'<a class="platform-link" href="{safe_href}" target="_blank" '
            f'rel="noopener noreferrer">{safe_label}</a>'
        ),
        unsafe_allow_html=True,
    )


def _render_direct_platform_link(label: str, target_url: str) -> None:
    safe_label = escape(str(label or "").strip())
    safe_href = escape(str(target_url or "").strip(), quote=True)
    base_app.st.markdown(
        (
            f'<a class="platform-link" href="{safe_href}" target="_blank" '
            f'rel="noopener noreferrer">{safe_label}</a>'
        ),
        unsafe_allow_html=True,
    )


def _handle_pending_tracked_open(
    *,
    default_browser_id: str,
    default_session_id: str,
    default_request_id: str,
    dataset_snapshot_id: str,
    default_traffic_source: str,
) -> bool:
    payload = _pending_tracked_open()
    if not payload:
        return False

    nonce = str(payload.get("analytics_nonce", "") or "").strip()
    handled_nonces = base_app.st.session_state.get("public_handled_open_nonces")
    if not isinstance(handled_nonces, set):
        handled_nonces = set()
        base_app.st.session_state["public_handled_open_nonces"] = handled_nonces
    if nonce and nonce in handled_nonces:
        return False

    browser_id = str(payload.get("analytics_browser_id", "") or "").strip() or default_browser_id
    session_id = str(payload.get("analytics_session_id", "") or "").strip() or default_session_id
    request_id = str(payload.get("analytics_request_id", "") or "").strip() or default_request_id or "unknown_request"
    traffic_source = str(payload.get("traffic_source", "") or "").strip() or default_traffic_source
    open_event = str(payload.get("open_event", "") or "").strip()
    target_url = str(payload.get("target_url", "") or "").strip()
    platform = str(payload.get("platform", "") or "").strip()
    track_name = str(payload.get("track_name", "") or "").strip()
    artist_name = str(payload.get("artist_name", "") or "").strip()
    playlist_position = str(payload.get("playlist_position", "") or "").strip()
    playlist_track_count = str(payload.get("playlist_track_count", "") or "").strip()
    coverage_ratio = str(payload.get("coverage_ratio", "") or "").strip()

    if open_event in {"spotify_opened", "youtube_opened"}:
        emit_product_event(
            "track_play_clicked",
            anonymous_browser_id=browser_id,
            session_id=session_id,
            request_id=request_id,
            dataset_snapshot_id=dataset_snapshot_id,
            traffic_source=traffic_source,
            track_name=track_name,
            artist_name=artist_name,
            platform=platform or ("spotify" if open_event == "spotify_opened" else "youtube"),
            playlist_position=int(playlist_position or 0),
        )
        emit_product_event(
            open_event,
            anonymous_browser_id=browser_id,
            session_id=session_id,
            request_id=request_id,
            dataset_snapshot_id=dataset_snapshot_id,
            traffic_source=traffic_source,
            track_name=track_name,
            playlist_position=int(playlist_position or 0),
        )
    elif open_event == "temp_playlist_opened":
        emit_product_event(
            "temp_playlist_opened",
            anonymous_browser_id=browser_id,
            session_id=session_id,
            request_id=request_id,
            dataset_snapshot_id=dataset_snapshot_id,
            traffic_source=traffic_source,
            playlist_track_count=int(playlist_track_count or 0),
            coverage_ratio=float(coverage_ratio or 0.0),
        )

    if nonce:
        handled_nonces.add(nonce)

    safe_target = escape(target_url, quote=True)
    base_app.components.html(
        (
            "<script>"
            f"(function(){{"
            f"var target='{safe_target}';"
            f"try {{ window.top.location.replace(target); return; }} catch (e) {{}}"
            f"try {{ window.parent.location.replace(target); return; }} catch (e) {{}}"
            f"window.location.replace(target);"
            f"}})();"
            "</script>"
            f'<meta http-equiv="refresh" content="0; url={safe_target}">'
            f'<div style="font-family: system-ui, sans-serif; color: #f5f5f5; background: #070707; padding: 12px;">'
            f'Opening destination... '
            f'<a href="{safe_target}" target="_top" rel="noopener noreferrer" '
            f'style="color: #d4af37;">Continue</a>'
            f"</div>"
        ),
        height=48,
    )
    base_app.st.caption("Opening destination...")
    base_app.st.stop()
    return True


def _emit_product_event_once(
    state_key: str,
    event_name: str,
    *,
    anonymous_browser_id: str,
    session_id: str,
    request_id: str,
    dataset_snapshot_id: str,
    page: str = "studio_home",
    traffic_source: str = "",
    **event_props: Any,
) -> None:
    state = _product_event_state()
    if state.get(state_key):
        return
    emit_product_event(
        event_name,
        anonymous_browser_id=anonymous_browser_id,
        session_id=session_id,
        request_id=request_id,
        dataset_snapshot_id=dataset_snapshot_id,
        page=page,
        traffic_source=traffic_source,
        **event_props,
    )
    state[state_key] = True


def _emit_product_event_if_changed(
    state_key: str,
    new_value: Any,
    event_name: str,
    *,
    anonymous_browser_id: str,
    session_id: str,
    request_id: str,
    dataset_snapshot_id: str,
    page: str = "studio_home",
    traffic_source: str = "",
    **event_props: Any,
) -> None:
    state = _product_event_state()
    if state.get(state_key) == new_value:
        return
    emit_product_event(
        event_name,
        anonymous_browser_id=anonymous_browser_id,
        session_id=session_id,
        request_id=request_id,
        dataset_snapshot_id=dataset_snapshot_id,
        page=page,
        traffic_source=traffic_source,
        **event_props,
    )
    state[state_key] = new_value


def _normalize_selection_values(values: list[Any] | tuple[Any, ...] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text:
            normalized.append(text)
    return normalized



def _normalize_mode(experience_mode: str) -> str:
    return "quick" if experience_mode == "Quick Mode" else "self_mix"



def _normalize_start_mode(experience_state: dict[str, Any]) -> str | None:
    if experience_state.get("experience_mode") == "Quick Mode":
        return "quick_mode"
    text = str(base_app.st.session_state.get("start_mode", "") or "").strip().lower()
    return text.replace(" ", "_") if text else None



def _generation_signature(
    seed_weight_items: tuple[tuple[str, float], ...],
    mood: str,
    spotify_weight_pct: int,
    discovery_hits_pct: int,
    target_minutes: int,
    preferred_artist_weight_items: tuple[tuple[str, float], ...],
    include_seed_tracks: bool,
) -> str:
    payload = repr(
        (
            seed_weight_items,
            mood,
            spotify_weight_pct,
            discovery_hits_pct,
            target_minutes,
            preferred_artist_weight_items,
            include_seed_tracks,
        )
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()



def _render_spotify_embed(track_id: str) -> None:
    embed_url = f"https://open.spotify.com/embed/track/{track_id}?utm_source=generator"
    phase2_app._ORIGINAL_COMPONENTS_HTML(
        f'<iframe src="{embed_url}" width="100%" height="152" frameborder="0" '
        'allowfullscreen="" allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture"></iframe>',
        height=170,
    )



def _queue_from_playlist(playlist: pd.DataFrame) -> pd.DataFrame:
    queue = playlist.copy().reset_index(drop=True)
    queue["position"] = queue.index + 1
    queue["duration_text"] = queue["duration_ms"].apply(base_app.format_track_duration)
    queue["spotify_url"] = queue.apply(lambda row: row.get("spotify_link") or row.get("url_spotify") or "", axis=1)
    queue["youtube_url"] = queue.apply(
        lambda row: base_app.prefer_direct_youtube_url(row.get("youtube_link"), row.get("url_youtube")),
        axis=1,
    )
    queue["queue_label"] = queue.apply(
        lambda row: f'{int(row["position"]):02d}. {row["artist"]} - {row["track"]} ({row["duration_text"]})',
        axis=1,
    )
    return queue


def _load_cached_queue(signature: str) -> pd.DataFrame | None:
    if base_app.st.session_state.get("public_result_signature") != signature:
        return None
    records = base_app.st.session_state.get("public_result_queue_records")
    if not isinstance(records, list) or not records:
        return None
    queue = pd.DataFrame.from_records(records)
    required_columns = {
        "position",
        "duration_text",
        "spotify_url",
        "youtube_url",
        "queue_label",
        "artist",
        "track",
        "duration_ms",
        "momentum_score",
    }
    if not required_columns.issubset(set(queue.columns)):
        return None
    return queue


def _store_cached_queue(signature: str, queue: pd.DataFrame) -> None:
    base_app.st.session_state["public_result_signature"] = signature
    base_app.st.session_state["public_result_queue_records"] = queue.to_dict("records")


def _build_temp_playlist_payload(queue: pd.DataFrame) -> dict[str, Any]:
    youtube_ids_result = phase2_app._ORIGINAL_BUILD_PLAYABLE_YOUTUBE_IDS(
        queue,
        max_ids=50,
        max_live_resolves=16,
        live_timeout=3,
        min_ids_required=2,
        fill_to_max=False,
        max_total_seconds=10.0,
        return_stats=True,
    )
    if isinstance(youtube_ids_result, tuple):
        youtube_ids, yt_stats = youtube_ids_result
    else:
        youtube_ids = youtube_ids_result
        yt_stats = {
            "playable_count": len(youtube_ids),
            "playable_linked_rows": len(youtube_ids),
            "target_rows": min(len(queue), 50),
            "included_direct": 0,
            "included_resolved": 0,
            "resolver_attempted": 0,
            "resolver_resolved": 0,
            "duplicate_rows": 0,
            "unresolved_rows": 0,
            "budget_blocked_rows": 0,
            "resolve_budget": 0,
            "row_diagnostics": [],
        }
    playlist_url = ""
    if len(youtube_ids) >= 2:
        playlist_url = "https://www.youtube.com/watch_videos?video_ids=" + ",".join(youtube_ids[:50])
    return {
        "playlist_url": playlist_url,
        "yt_stats": yt_stats,
    }


def _load_or_build_temp_playlist_payload(signature: str, queue: pd.DataFrame) -> dict[str, Any]:
    cached_signature = str(base_app.st.session_state.get("public_temp_playlist_signature", "") or "").strip()
    cached_payload = base_app.st.session_state.get("public_temp_playlist_payload")
    if cached_signature == signature and isinstance(cached_payload, dict):
        return cached_payload

    payload = _build_temp_playlist_payload(queue)
    base_app.st.session_state["public_temp_playlist_signature"] = signature
    base_app.st.session_state["public_temp_playlist_payload"] = payload
    yt_stats = payload.get("yt_stats", {}) if isinstance(payload.get("yt_stats"), dict) else {}
    base_app.st.session_state["_yt_playlist_row_diagnostics"] = (
        yt_stats.get("row_diagnostics", []) if isinstance(yt_stats.get("row_diagnostics", []), list) else []
    )
    return payload


def _reset_public_cached_state() -> None:
    for key in [
        "public_result_signature",
        "public_result_queue_records",
        "public_selected_position",
        "public_temp_playlist_signature",
        "public_temp_playlist_payload",
        "public_request_id",
        "public_request_log_pending",
        "public_logged_signature",
    ]:
        base_app.st.session_state.pop(key, None)


def _ensure_public_cache_version() -> None:
    current = str(base_app.st.session_state.get("public_ui_cache_version", "") or "").strip()
    if current != PUBLIC_UI_CACHE_VERSION:
        _reset_public_cached_state()
        base_app.st.session_state["public_ui_cache_version"] = PUBLIC_UI_CACHE_VERSION


def main() -> None:
    app_boot_started_at = time.perf_counter()
    patch_base_app_for_phase2()

    base_app.st.set_page_config(page_title="DJ Mixing Station Studio", page_icon="🎵", layout="wide")
    base_app.apply_theme()

    base_app.st.markdown(
        """
        <div class="hero">
          <h1>DJ Mixing Station Studio</h1>
          <p>Shape your sound with smart mood controls and cross-platform signal blending.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    try:
        raw_df, source_path, dataset_path = read_source_phase2(None, base_app.DEFAULT_DATASET_ID)
        data = base_app.prepare_music_data_cached(raw_df)
        _render_hidden_startup_health("Healthy")
    except Exception as exc:
        _render_hidden_startup_health("Unhealthy")
        base_app.st.error(str(exc))
        base_app.st.stop()

    if data.empty:
        _render_hidden_startup_health("Unhealthy")
        base_app.st.error("No usable rows found after cleaning the dataset.")
        base_app.st.stop()

    dataset_snapshot_id = _dataset_snapshot_id(source_path)
    anonymous_browser_id = _anonymous_browser_id()
    session_id = _session_id()
    traffic_source = _product_traffic_source()
    _handle_pending_tracked_open(
        default_browser_id=anonymous_browser_id,
        default_session_id=session_id,
        default_request_id=str(base_app.st.session_state.get("public_request_id", "") or "").strip(),
        dataset_snapshot_id=dataset_snapshot_id,
        default_traffic_source=traffic_source,
    )
    _ensure_public_cache_version()
    request_id = str(base_app.st.session_state.get("public_request_id", "") or "").strip()

    _emit_product_event_once(
        "session_started",
        "session_started",
        anonymous_browser_id=anonymous_browser_id,
        session_id=session_id,
        request_id=request_id,
        dataset_snapshot_id=dataset_snapshot_id,
        traffic_source=traffic_source,
        landing_path="studio_home",
        referrer="unknown",
        user_agent_family="unknown",
    )
    _emit_product_event_once(
        "landing_viewed",
        "landing_viewed",
        anonymous_browser_id=anonymous_browser_id,
        session_id=session_id,
        request_id=request_id,
        dataset_snapshot_id=dataset_snapshot_id,
        traffic_source=traffic_source,
        page_load_ms=int((time.perf_counter() - app_boot_started_at) * 1000),
    )

    seed_weights, experience_state = base_app.render_seed_experience(data)

    with base_app.st.sidebar:
        base_app.st.header("Recommendation Controls")
        if experience_state.get("experience_mode") == "Quick Mode":
            selected_quick_vibe = str(experience_state.get("quick_vibe", "Focus Flow"))
            quick_defaults = base_app.QUICK_VIBE_DEFAULTS.get(selected_quick_vibe, base_app.QUICK_VIBE_DEFAULTS["Focus Flow"])
            if base_app.st.session_state.get("quick_vibe_applied") != selected_quick_vibe:
                base_app.st.session_state["quick_spotify_weight_pct"] = int(quick_defaults["spotify_weight_pct"])
                base_app.st.session_state["quick_discovery_hits_pct"] = int(quick_defaults["discovery_hits_pct"])
                base_app.st.session_state["quick_vibe_applied"] = selected_quick_vibe

            mood = str(quick_defaults["mood"])
            spotify_weight_pct = base_app.st.slider("Platform Bias", 0, 100, key="quick_spotify_weight_pct")
            youtube_weight_pct = 100 - spotify_weight_pct
            base_app.st.caption(f"Spotify ({spotify_weight_pct}%) <- -> YouTube ({youtube_weight_pct}%)")
            discovery_hits_pct = base_app.st.slider("Discovery Mode", 0, 100, key="quick_discovery_hits_pct")
            hidden_gems_pct = 100 - discovery_hits_pct
            base_app.st.caption(f"Hits ({discovery_hits_pct}%) <- -> Hidden Gems ({hidden_gems_pct}%)")
        else:
            self_mix_vibe = base_app.st.selectbox(
                "Vibe Options",
                options=list(base_app.PERSONALITY_PROFILES.keys()),
                index=0,
                key="self_mix_vibe_option",
            )
            mood = str(base_app.QUICK_VIBE_DEFAULTS.get(self_mix_vibe, base_app.QUICK_VIBE_DEFAULTS["Focus Flow"])["mood"])
            if "spotify_weight_pct" not in base_app.st.session_state:
                base_app.st.session_state["spotify_weight_pct"] = 65
            spotify_weight_pct = base_app.st.slider("Platform Bias", 0, 100, key="spotify_weight_pct")
            youtube_weight_pct = 100 - spotify_weight_pct
            base_app.st.caption(f"Spotify ({spotify_weight_pct}%) <- -> YouTube ({youtube_weight_pct}%)")
            if "discovery_hits_pct" not in base_app.st.session_state:
                base_app.st.session_state["discovery_hits_pct"] = 65
            discovery_hits_pct = base_app.st.slider("Discovery Mode", 0, 100, key="discovery_hits_pct")
            hidden_gems_pct = 100 - discovery_hits_pct
            base_app.st.caption(f"Hits ({discovery_hits_pct}%) <- -> Hidden Gems ({hidden_gems_pct}%)")

        target_minutes = base_app.st.slider("Total Playlist Minutes (±3 mins)", 30, 300, 120)
        lower_window = target_minutes - base_app.PLAYLIST_TOLERANCE_MINUTES
        upper_window = target_minutes + base_app.PLAYLIST_TOLERANCE_MINUTES

    sidebar_playlist_caption = (
        f"Playlist window: {lower_window} to {upper_window} mins "
        f"({base_app.format_hours_minutes(lower_window)} to {base_app.format_hours_minutes(upper_window)})"
    )
    sidebar_temp_playlist_payload: dict[str, Any] | None = None
    sidebar_temp_playlist_duplicate_collapsed = 0

    seed_weight_items = tuple(sorted((str(name), float(weight)) for name, weight in seed_weights.items()))
    preferred_artist_weight_items: tuple[tuple[str, float], ...] = ()
    if experience_state.get("experience_mode") == "Self Mix":
        preferred_artist_weight_items = tuple(sorted(base_app._build_preferred_artist_weights(data).items()))
    include_seed_tracks = discovery_hits_pct == 100

    normalized_mode = _normalize_mode(str(experience_state.get("experience_mode", "Self Mix")))
    normalized_start_mode = _normalize_start_mode(experience_state)
    vibe_option = str(experience_state.get("quick_vibe") or base_app.st.session_state.get("self_mix_vibe_option") or "")
    _emit_product_event_if_changed(
        "mode_selected",
        (normalized_mode, normalized_start_mode, vibe_option),
        "mode_selected",
        anonymous_browser_id=anonymous_browser_id,
        session_id=session_id,
        request_id=request_id,
        dataset_snapshot_id=dataset_snapshot_id,
        traffic_source=traffic_source,
        mode=normalized_mode,
        start_mode=normalized_start_mode,
        vibe_option=vibe_option,
    )

    control_state = {
        "platform_bias_spotify_pct": int(spotify_weight_pct),
        "discovery_hits_pct": int(discovery_hits_pct),
        "target_minutes": int(target_minutes),
        "vibe_option": vibe_option,
    }
    previous_control_state = _product_event_state().get("control_state", {})
    if not isinstance(previous_control_state, dict):
        previous_control_state = {}
    for control_name, control_value in control_state.items():
        if previous_control_state.get(control_name) != control_value:
            emit_product_event(
                "controls_changed",
                anonymous_browser_id=anonymous_browser_id,
                session_id=session_id,
                request_id=request_id,
                dataset_snapshot_id=dataset_snapshot_id,
                traffic_source=traffic_source,
                control_name=control_name,
                control_value=control_value,
            )
    _product_event_state()["control_state"] = dict(control_state)

    current_selected_artists = _normalize_selection_values(base_app.st.session_state.get("selected_seed_artists", []))
    current_selected_songs = _normalize_selection_values(base_app.st.session_state.get("selected_seed_songs", []))
    previous_selected_artists = set(_product_event_state().get("selected_seed_artists", []))
    previous_selected_songs = set(_product_event_state().get("selected_seed_songs", []))
    for artist_name in current_selected_artists:
        if artist_name not in previous_selected_artists:
            emit_product_event(
                "artist_selected",
                anonymous_browser_id=anonymous_browser_id,
                session_id=session_id,
                request_id=request_id,
                dataset_snapshot_id=dataset_snapshot_id,
                traffic_source=traffic_source,
                artist_name=artist_name,
                selection_count_after=len(current_selected_artists),
            )
    for song_name in current_selected_songs:
        if song_name not in previous_selected_songs:
            emit_product_event(
                "song_selected",
                anonymous_browser_id=anonymous_browser_id,
                session_id=session_id,
                request_id=request_id,
                dataset_snapshot_id=dataset_snapshot_id,
                traffic_source=traffic_source,
                track_name=song_name,
                selection_count_after=len(current_selected_songs),
            )
    _product_event_state()["selected_seed_artists"] = list(current_selected_artists)
    _product_event_state()["selected_seed_songs"] = list(current_selected_songs)

    generation_signature = _generation_signature(
        seed_weight_items=seed_weight_items,
        mood=mood,
        spotify_weight_pct=spotify_weight_pct,
        discovery_hits_pct=discovery_hits_pct,
        target_minutes=target_minutes,
        preferred_artist_weight_items=preferred_artist_weight_items,
        include_seed_tracks=include_seed_tracks,
    )
    if base_app.st.session_state.get("public_logged_signature") != generation_signature:
        request_id = str(uuid.uuid4())
        base_app.st.session_state["public_logged_signature"] = generation_signature
        base_app.st.session_state["public_request_id"] = request_id
        base_app.st.session_state["public_request_log_pending"] = True

    request_id = str(base_app.st.session_state.get("public_request_id", "") or "").strip()
    should_emit_request_logs = bool(request_id) and bool(base_app.st.session_state.get("public_request_log_pending", False))
    request_started_at = time.perf_counter()
    queue = _load_cached_queue(generation_signature)

    if queue is None:
        if should_emit_request_logs:
            emit_request_received(
                request_id=request_id,
                session_id=session_id,
                dataset_snapshot_id=dataset_snapshot_id,
                mode=_normalize_mode(str(experience_state.get("experience_mode", "Self Mix"))),
                start_mode=_normalize_start_mode(experience_state),
                target_minutes=target_minutes,
                tolerance_minutes=base_app.PLAYLIST_TOLERANCE_MINUTES,
                platform_bias_spotify_pct=spotify_weight_pct,
                discovery_hits_pct=discovery_hits_pct,
                vibe_option=str(experience_state.get("quick_vibe") or base_app.st.session_state.get("self_mix_vibe_option") or ""),
                final_pick_count=len(seed_weight_items),
            )
            emit_seed_resolution_completed(
                request_id=request_id,
                session_id=session_id,
                dataset_snapshot_id=dataset_snapshot_id,
                selected_artists_count=len(base_app.st.session_state.get("selected_seed_artists", [])),
                selected_songs_count=len(base_app.st.session_state.get("selected_seed_songs", [])),
                seed_display_names=[str(name) for name, _weight in seed_weight_items],
                seed_artist_weights={str(name): float(weight) for name, weight in preferred_artist_weight_items},
            )

        try:
            ranking_started_at = time.perf_counter()
            recommendations = base_app.recommend_tracks_cached(
                data=data,
                seed_weight_items=seed_weight_items,
                mood=mood,
                spotify_weight=spotify_weight_pct / 100.0,
                discovery_mode=hidden_gems_pct / 100.0,
                top_k=180,
                exclude_seed_tracks=not include_seed_tracks,
                preferred_artist_weight_items=preferred_artist_weight_items,
            )
            ranking_latency_ms = int((time.perf_counter() - ranking_started_at) * 1000)
            if should_emit_request_logs:
                top_display_names = recommendations.get("display_name", pd.Series(dtype=str)).astype(str).head(5).tolist()
                emit_ranking_completed(
                    request_id=request_id,
                    session_id=session_id,
                    dataset_snapshot_id=dataset_snapshot_id,
                    candidate_pool_size=len(data),
                    ranked_count=len(recommendations),
                    top_display_names=top_display_names,
                    latency_ms=ranking_latency_ms,
                )

            playlist_started_at = time.perf_counter()
            playlist = base_app.build_duration_playlist_cached(
                recommendations=recommendations,
                target_minutes=target_minutes,
                tolerance_minutes=base_app.PLAYLIST_TOLERANCE_MINUTES,
                candidate_limit=180,
                max_tracks=50,
            )
            playlist_latency_ms = int((time.perf_counter() - playlist_started_at) * 1000)
        except Exception as exc:
            emit_product_event(
                "playlist_generation_failed",
                anonymous_browser_id=anonymous_browser_id,
                session_id=session_id,
                request_id=request_id,
                dataset_snapshot_id=dataset_snapshot_id,
                traffic_source=traffic_source,
                failure_reason="request_pipeline_failure",
                error_type=type(exc).__name__,
                time_to_playlist_ms=int((time.perf_counter() - request_started_at) * 1000),
            )
            if should_emit_request_logs:
                emit_error(
                    request_id=request_id,
                    session_id=session_id,
                    dataset_snapshot_id=dataset_snapshot_id,
                    error_type=type(exc).__name__,
                    error_code="REQUEST_PIPELINE_FAILURE",
                    error_message=str(exc),
                    stage="ranking_or_playlist",
                    latency_ms=int((time.perf_counter() - request_started_at) * 1000),
                )
                base_app.st.session_state["public_request_log_pending"] = False
            raise

        playlist["mood_tag"] = mood
        if should_emit_request_logs:
            playlist_total_seconds = int((playlist.get("duration_ms", pd.Series(dtype=int)).fillna(0).sum()) / 1000)
            target_seconds = int(target_minutes * 60)
            window_min_seconds = int((target_minutes - base_app.PLAYLIST_TOLERANCE_MINUTES) * 60)
            window_max_seconds = int((target_minutes + base_app.PLAYLIST_TOLERANCE_MINUTES) * 60)
            emit_playlist_optimized(
                request_id=request_id,
                session_id=session_id,
                dataset_snapshot_id=dataset_snapshot_id,
                playlist_track_count=len(playlist),
                playlist_total_seconds=playlist_total_seconds,
                target_seconds=target_seconds,
                window_min_seconds=window_min_seconds,
                window_max_seconds=window_max_seconds,
                in_target_window=window_min_seconds <= playlist_total_seconds <= window_max_seconds,
                optimizer_latency_ms=playlist_latency_ms,
            )

        if playlist.empty:
            emit_product_event(
                "playlist_generation_failed",
                anonymous_browser_id=anonymous_browser_id,
                session_id=session_id,
                request_id=request_id,
                dataset_snapshot_id=dataset_snapshot_id,
                traffic_source=traffic_source,
                failure_reason="empty_playlist",
                time_to_playlist_ms=int((time.perf_counter() - request_started_at) * 1000),
            )
            if should_emit_request_logs:
                emit_error(
                    request_id=request_id,
                    session_id=session_id,
                    dataset_snapshot_id=dataset_snapshot_id,
                    error_type="NoPlaylistGenerated",
                    error_code="EMPTY_PLAYLIST",
                    error_message="No playlist could be generated for this duration target.",
                    stage="playlist_optimized",
                    latency_ms=int((time.perf_counter() - request_started_at) * 1000),
                )
                base_app.st.session_state["public_request_log_pending"] = False
            base_app.st.warning("No playlist could be generated for this duration target. Try different seed selections.")
            base_app.st.stop()

        queue = _queue_from_playlist(playlist)
        _store_cached_queue(generation_signature, queue)
        base_app.st.session_state.pop("public_temp_playlist_signature", None)
        base_app.st.session_state.pop("public_temp_playlist_payload", None)
        playlist_total_minutes = int(queue.get("duration_ms", pd.Series(dtype=int)).fillna(0).sum() / 60000)
        emit_product_event(
            "playlist_generated",
            anonymous_browser_id=anonymous_browser_id,
            session_id=session_id,
            request_id=request_id,
            dataset_snapshot_id=dataset_snapshot_id,
            traffic_source=traffic_source,
            track_count=len(queue),
            playlist_minutes_target=int(target_minutes),
            playlist_minutes_actual=int(playlist_total_minutes),
            platform_bias_spotify_pct=int(spotify_weight_pct),
            discovery_hits_pct=int(discovery_hits_pct),
            selected_artists_count=len(current_selected_artists),
            selected_songs_count=len(current_selected_songs),
            time_to_playlist_ms=int((time.perf_counter() - request_started_at) * 1000),
        )

    temp_playlist_payload = _load_or_build_temp_playlist_payload(generation_signature, queue)
    yt_stats = temp_playlist_payload.get("yt_stats", {}) if isinstance(temp_playlist_payload.get("yt_stats"), dict) else {}
    target_rows = int(yt_stats.get("target_rows", 0) or 0)
    playable_rows = int(yt_stats.get("playable_count", 0) or 0)
    linked_rows = int(yt_stats.get("playable_linked_rows", playable_rows) or playable_rows)
    duplicate_collapsed = max(0, linked_rows - playable_rows)
    sidebar_temp_playlist_payload = temp_playlist_payload
    sidebar_temp_playlist_duplicate_collapsed = duplicate_collapsed
    unresolved_rows = int(yt_stats.get("unresolved_rows", 0) or 0)
    resolver_attempted = int(yt_stats.get("resolver_attempted", 0) or 0)
    resolver_resolved = int(yt_stats.get("resolver_resolved", 0) or 0)
    included_direct = int(yt_stats.get("included_direct", 0) or 0)
    included_resolved = int(yt_stats.get("included_resolved", 0) or 0)
    budget_blocked_rows = int(yt_stats.get("budget_blocked_rows", 0) or 0)
    resolver_degradation_reason = _resolver_degradation_reason(
        yt_stats,
        target_rows=target_rows,
        linked_rows=linked_rows,
    )
    resolver_degraded = resolver_degradation_reason != "healthy"
    if should_emit_request_logs:
        emit_resolver_quality_evaluated(
            request_id=request_id,
            session_id=session_id,
            dataset_snapshot_id=dataset_snapshot_id,
            target_rows=target_rows,
            linked_rows=linked_rows,
            playable_rows=playable_rows,
            unresolved_rows=unresolved_rows,
            resolver_attempted=resolver_attempted,
            resolver_resolved=resolver_resolved,
            included_direct=included_direct,
            included_resolved=included_resolved,
            budget_blocked_rows=budget_blocked_rows,
            degraded=resolver_degraded,
            degradation_reason=resolver_degradation_reason,
        )

    player_col, mode_col = base_app.st.columns([3, 2])
    with player_col:
        selected_label = base_app.st.selectbox("Now Playing", options=queue["queue_label"].tolist())
    with mode_col:
        playback_platform = base_app.st.radio("Playback", options=["Auto", "Spotify", "YouTube"], horizontal=True)

    selected_row = queue.loc[queue["queue_label"] == selected_label].iloc[0]
    selected_spotify = str(selected_row["spotify_url"]).strip()
    selected_youtube, selected_youtube_watch = base_app.resolve_playback_youtube_targets(selected_row)
    selected_youtube = str(selected_youtube).strip()
    selected_youtube_watch = str(selected_youtube_watch).strip()
    selected_spotify_track_id = base_app.extract_spotify_track_id(selected_spotify)
    youtube_embed_blocked = False

    if (playback_platform in {"YouTube", "Auto"}) and not selected_youtube_watch:
        forced_link, forced_embed = base_app.resolve_playback_youtube_targets(selected_row, force_live=True, live_timeout=4)
        forced_link = str(forced_link).strip()
        forced_embed = str(forced_embed).strip()
        if forced_link:
            selected_youtube = forced_link
        if forced_embed:
            selected_youtube_watch = forced_embed

    if selected_youtube_watch and playback_platform in {"YouTube", "Auto"}:
        try:
            if not base_app._is_youtube_embed_likely_available(selected_youtube_watch):
                youtube_embed_blocked = True
                selected_youtube_watch = ""
        except Exception:
            pass

    base_app.st.markdown(
        f'**{selected_row["artist"]} - {selected_row["track"]}** · {selected_row["duration_text"]} '
        f'· Momentum {selected_row["momentum_score"]:.3f}'
    )
    if selected_spotify:
        _render_direct_platform_link("Spotify", selected_spotify)
    if selected_youtube:
        _render_direct_platform_link("YouTube", selected_youtube)

    if playback_platform == "YouTube":
        if selected_youtube_watch:
            phase2_app._ORIGINAL_ST_VIDEO(selected_youtube_watch)
        elif selected_spotify_track_id:
            if youtube_embed_blocked:
                base_app.st.info("This YouTube video can't be embedded here. Falling back to Spotify player.")
            else:
                base_app.st.info("YouTube embed unavailable for this track. Falling back to Spotify player.")
            _render_spotify_embed(selected_spotify_track_id)
        elif selected_youtube:
            if youtube_embed_blocked:
                base_app.st.info("This YouTube video can't be embedded here. Use the YouTube button.")
            else:
                base_app.st.info("No direct YouTube video ID available for embed. Use the YouTube button.")
    elif playback_platform == "Spotify":
        if selected_spotify_track_id:
            _render_spotify_embed(selected_spotify_track_id)
        elif selected_spotify:
            base_app.st.info("No direct Spotify track ID available for embed. Use the Spotify button.")
    else:
        if selected_youtube_watch:
            phase2_app._ORIGINAL_ST_VIDEO(selected_youtube_watch)
        elif selected_spotify_track_id:
            _render_spotify_embed(selected_spotify_track_id)

    if should_emit_request_logs:
        emit_response_sent(
            request_id=request_id,
            session_id=session_id,
            dataset_snapshot_id=dataset_snapshot_id,
            playlist_track_count=len(queue),
            latency_ms=int((time.perf_counter() - request_started_at) * 1000),
        )
        base_app.st.session_state["public_request_log_pending"] = False

    with base_app.st.sidebar:
        base_app.st.caption(sidebar_playlist_caption)
        if sidebar_temp_playlist_payload is not None:
            playlist_url = str(sidebar_temp_playlist_payload.get("playlist_url", "") or "").strip()
            sidebar_yt_stats = (
                sidebar_temp_playlist_payload.get("yt_stats", {})
                if isinstance(sidebar_temp_playlist_payload.get("yt_stats"), dict)
                else {}
            )
            sidebar_target_rows = int(sidebar_yt_stats.get("target_rows", 0) or 0)
            sidebar_playable_rows = int(sidebar_yt_stats.get("playable_count", 0) or 0)
            sidebar_linked_rows = int(
                sidebar_yt_stats.get("playable_linked_rows", sidebar_playable_rows) or sidebar_playable_rows
            )
            if playlist_url:
                base_app.st.markdown(
                    (
                        f'<a class="platform-link" href="{escape(playlist_url, quote=True)}" target="_blank" '
                        'rel="noopener noreferrer">Open Full Temporary YouTube Playlist</a>'
                    ),
                    unsafe_allow_html=True,
                )
                base_app.st.caption("This opens the whole generated YouTube playlist, not just the current Now Playing track.")
            else:
                base_app.st.caption("Temporary YouTube playlist link requires at least 2 playable YouTube IDs.")
            if sidebar_target_rows > 0:
                coverage_pct = (sidebar_linked_rows / sidebar_target_rows) * 100.0
                base_app.st.caption(
                    "Temporary playlist coverage: "
                    f"{sidebar_linked_rows}/{sidebar_target_rows} rows linked ({coverage_pct:.0f}%). "
                    f"Unique YouTube IDs: {sidebar_playable_rows}. "
                    f"Direct: {int(sidebar_yt_stats.get('included_direct', 0) or 0)} · "
                    f"Resolved: {int(sidebar_yt_stats.get('included_resolved', 0) or 0)} · "
                    f"Resolver attempts: {int(sidebar_yt_stats.get('resolver_attempted', 0) or 0)}."
                )
                if sidebar_temp_playlist_duplicate_collapsed > 0:
                    base_app.st.caption(f"Duplicate IDs collapsed: {sidebar_temp_playlist_duplicate_collapsed}")


if __name__ == "__main__":
    main()
