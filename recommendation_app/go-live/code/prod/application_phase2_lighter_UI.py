from __future__ import annotations

import hashlib
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pandas as pd

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import phase2_runtime_config as cfg
from prod.application_phase2 import patch_base_app_for_phase2, base_app, read_source_phase2
from prod.go_live_structured_logging import (
    emit_error,
    emit_playlist_optimized,
    emit_ranking_completed,
    emit_request_received,
    emit_response_sent,
    emit_seed_resolution_completed,
)


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
    base_app.components.html(
        f'<iframe src="{embed_url}" width="100%" height="152" frameborder="0" '
        'allowfullscreen="" allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture"></iframe>',
        height=170,
    )



def main() -> None:
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
    session_id = _session_id()

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
        base_app.st.caption(
            f"Playlist window: {lower_window} to {upper_window} mins "
            f"({base_app.format_hours_minutes(lower_window)} to {base_app.format_hours_minutes(upper_window)})"
        )

    seed_weight_items = tuple(sorted((str(name), float(weight)) for name, weight in seed_weights.items()))
    preferred_artist_weight_items: tuple[tuple[str, float], ...] = ()
    if experience_state.get("experience_mode") == "Self Mix":
        preferred_artist_weight_items = tuple(sorted(base_app._build_preferred_artist_weights(data).items()))
    include_seed_tracks = discovery_hits_pct == 100

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
    total_playlist_minutes = pd.to_numeric(playlist.get("duration_ms"), errors="coerce").fillna(0).sum() / 60_000
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

    left, mid, right = base_app.st.columns(3)
    with left:
        base_app.render_metric_card("Tracks Available", f"{len(data):,}")
    with mid:
        base_app.render_metric_card("Artists Available", f"{data['artist'].nunique():,}")
    with right:
        base_app.render_metric_card("Playlist Duration", base_app.format_total_duration(total_playlist_minutes))

    if playlist.empty:
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
    if not (lower_window <= total_playlist_minutes <= upper_window):
        base_app.st.warning(
            f"No exact playlist found in {lower_window}-{upper_window} mins. "
            f"Showing closest match: {base_app.format_total_duration(total_playlist_minutes)}."
        )

    base_app.st.subheader("Playable Playlist")
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

    if (playback_platform in {"YouTube", "Auto"}) and not selected_youtube_watch:
        forced_link, forced_embed = base_app.resolve_playback_youtube_targets(selected_row, force_live=True, live_timeout=4)
        forced_link = str(forced_link).strip()
        forced_embed = str(forced_embed).strip()
        if forced_link:
            selected_youtube = forced_link
        if forced_embed:
            selected_youtube_watch = forced_embed

    base_app.st.markdown(
        f'**{selected_row["artist"]} - {selected_row["track"]}** · {selected_row["duration_text"]} '
        f'· Momentum {selected_row["momentum_score"]:.3f}'
    )
    base_app.render_track_links(selected_spotify, selected_youtube)

    if playback_platform == "YouTube":
        if selected_youtube_watch:
            base_app.st.video(selected_youtube_watch)
        elif selected_spotify_track_id:
            base_app.st.info("YouTube embed unavailable for this track. Falling back to Spotify player.")
            _render_spotify_embed(selected_spotify_track_id)
        elif selected_youtube:
            base_app.st.info("No direct YouTube video ID available for embed. Use the YouTube button.")
    elif playback_platform == "Spotify":
        if selected_spotify_track_id:
            _render_spotify_embed(selected_spotify_track_id)
        elif selected_spotify:
            base_app.st.info("No direct Spotify track ID available for embed. Use the Spotify button.")
    else:
        if selected_youtube_watch:
            base_app.st.video(selected_youtube_watch)
        elif selected_spotify_track_id:
            _render_spotify_embed(selected_spotify_track_id)

    with base_app.st.spinner("Building temporary YouTube playlist from playable URLs..."):
        youtube_ids_result = base_app.build_playable_youtube_ids(
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

    if len(youtube_ids) >= 2:
        temp_youtube_playlist = "https://www.youtube.com/watch_videos?video_ids=" + ",".join(youtube_ids[:50])
        base_app.render_platform_link("Open Temporary YouTube Playlist", temp_youtube_playlist)
    else:
        base_app.st.caption("Temporary YouTube playlist link requires at least 2 playable YouTube IDs.")
    target_rows = int(yt_stats.get("target_rows", 0) or 0)
    playable_rows = int(yt_stats.get("playable_count", 0) or 0)
    linked_rows = int(yt_stats.get("playable_linked_rows", playable_rows) or playable_rows)
    if target_rows > 0:
        coverage_pct = (linked_rows / target_rows) * 100.0
        duplicate_collapsed = max(0, linked_rows - playable_rows)
        base_app.st.caption(
            "Temporary playlist coverage: "
            f"{linked_rows}/{target_rows} rows linked ({coverage_pct:.0f}%). "
            f"Unique YouTube IDs: {playable_rows}. "
            f"Direct: {int(yt_stats.get('included_direct', 0) or 0)} · "
            f"Resolved: {int(yt_stats.get('included_resolved', 0) or 0)} · "
            f"Resolver attempts: {int(yt_stats.get('resolver_attempted', 0) or 0)}."
        )
        if duplicate_collapsed > 0:
            base_app.st.caption(f"Duplicate IDs collapsed: {duplicate_collapsed}")
    base_app.st.session_state["_yt_playlist_row_diagnostics"] = (
        yt_stats.get("row_diagnostics", []) if isinstance(yt_stats.get("row_diagnostics", []), list) else []
    )

    if should_emit_request_logs:
        emit_response_sent(
            request_id=request_id,
            session_id=session_id,
            dataset_snapshot_id=dataset_snapshot_id,
            playlist_track_count=len(queue),
            latency_ms=int((time.perf_counter() - request_started_at) * 1000),
        )
        base_app.st.session_state["public_request_log_pending"] = False


if __name__ == "__main__":
    main()
