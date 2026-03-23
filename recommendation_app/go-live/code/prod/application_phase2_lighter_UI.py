from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

import pandas as pd

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import phase2_runtime_config as cfg
from phase2_managed_loader import load_prepared_dataset, validate_prepared_dataset

if str(cfg.OFFLINE_V2_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(cfg.OFFLINE_V2_CODE_DIR))
OFFLINE_V2_MODE_DIR = cfg.OFFLINE_V2_CODE_DIR / "prod"
if str(OFFLINE_V2_MODE_DIR) not in sys.path:
    sys.path.insert(0, str(OFFLINE_V2_MODE_DIR))

import application_v2 as app_v2  # type: ignore  # noqa: E402


base_app = app_v2.base_app


@base_app.st.cache_data(show_spinner=False)
def load_phase2_dataset_cached(version: str, dataset_root: str) -> tuple[pd.DataFrame, str]:
    frame, release = load_prepared_dataset(version=version or None, dataset_root=Path(dataset_root))
    return frame, release.version


def read_source_phase2(_uploaded_file: Any, _dataset_id: str) -> tuple[pd.DataFrame, str, str]:
    dataset_root = cfg.dataset_root()
    prepared_df, release_version = load_phase2_dataset_cached(
        version=cfg.DATASET_VERSION or "",
        dataset_root=str(dataset_root),
    )
    source_path = f"managed_snapshot::{release_version}"
    dataset_path = str((dataset_root / "snapshots" / release_version).resolve())
    return prepared_df, source_path, dataset_path


def prepare_music_data_cached_phase2(raw_df: pd.DataFrame) -> pd.DataFrame:
    try:
        return validate_prepared_dataset(raw_df.copy())
    except Exception:
        return app_v2.prepare_music_data_cached_v2(raw_df)


def recommend_tracks_phase2(
    data: pd.DataFrame,
    seed_weight_items: tuple[tuple[str, float], ...],
    mood: str,
    spotify_weight: float,
    discovery_mode: float,
    top_k: int,
    exclude_seed_tracks: bool,
    preferred_artist_weight_items: tuple[tuple[str, float], ...],
) -> pd.DataFrame:
    seed_weights = {str(name): float(weight) for name, weight in seed_weight_items}
    preferred_artist_weights = {str(name): float(weight) for name, weight in preferred_artist_weight_items}
    return app_v2.recommend_tracks(
        data=data,
        seed_display_name=seed_weights,
        mood=mood,
        spotify_weight=spotify_weight,
        discovery_mode=discovery_mode,
        top_k=top_k,
        exclude_seed_tracks=exclude_seed_tracks,
        preferred_artist_weights=preferred_artist_weights,
    )


def build_duration_playlist_phase2(
    recommendations: pd.DataFrame,
    target_minutes: int,
    tolerance_minutes: int,
    candidate_limit: int,
    max_tracks: int,
) -> pd.DataFrame:
    return app_v2.build_duration_playlist(
        recommendations=recommendations,
        target_minutes=target_minutes,
        tolerance_minutes=tolerance_minutes,
        candidate_limit=candidate_limit,
        max_tracks=max_tracks,
    )


def get_seed_ui_options_phase2(data: pd.DataFrame) -> tuple[list[str], list[str], list[str], list[str]]:
    song_options = sorted(data["display_name"].astype(str).unique().tolist())
    artist_options = app_v2._artist_options_with_credits(data)
    quick_top_songs = base_app.top_song_seed_options(data, top_n=8)
    quick_top_artists = base_app.top_artist_seed_options(data, top_n=8)["artist"].astype(str).tolist()
    return song_options, artist_options, quick_top_songs, quick_top_artists


def patch_base_app_for_phase2_lighter() -> None:
    app_v2.patch_base_app_for_v2()
    base_app.DEFAULT_DATASET_ID = "managed://latest"
    base_app.read_source = read_source_phase2
    base_app.prepare_music_data_cached = prepare_music_data_cached_phase2
    base_app.recommend_tracks_cached = recommend_tracks_phase2
    base_app.build_duration_playlist_cached = build_duration_playlist_phase2
    base_app.get_seed_ui_options = get_seed_ui_options_phase2


def _render_seed_choice_chips(label: str, options: list[str], state_key: str) -> None:
    base_app.st.caption(label)
    clicked = base_app.render_toggle_chips(
        options,
        selected_values=list(base_app.st.session_state[state_key]),
        key_prefix=f"lighter_{state_key}",
        min_chips=5,
    )
    if clicked:
        base_app._toggle_seed_selection(state_key, clicked, base_app.MAX_SEED_SELECTIONS)


def _render_seed_choice_checkboxes(label: str, options: list[str], state_key: str) -> list[str]:
    base_app.st.caption(label)
    selected_values = set(base_app.st.session_state.get(state_key, []))
    chip_count = max(5, min(len(options), 8))
    visible_options = options[:chip_count]
    chosen: list[str] = []

    for row_start in range(0, len(visible_options), 3):
        row_labels = visible_options[row_start : row_start + 3]
        columns = base_app.st.columns(len(row_labels))
        for idx, option in enumerate(row_labels):
            checked = columns[idx].checkbox(
                option,
                value=option in selected_values,
                key=f"lighter_form_{state_key}_{row_start + idx}",
            )
            if checked:
                chosen.append(option)

    return chosen[: base_app.MAX_SEED_SELECTIONS]


def _queue_signature(queue: pd.DataFrame, mood: str, target_minutes: int) -> str:
    payload = "|".join(
        queue.get("queue_label", pd.Series(dtype=str)).astype(str).tolist()
        + [str(mood), str(target_minutes)]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _pick_surprise_seed(data: pd.DataFrame, counter: int) -> str:
    candidates = data.sort_values(["momentum_score", "views", "stream"], ascending=False).head(250)
    picked_seed = candidates.sample(1, random_state=9000 + counter).iloc[0]["display_name"]
    return str(picked_seed)


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


def _render_queue_table(queue: pd.DataFrame) -> None:
    lines: list[str] = []
    for row in queue.itertuples(index=False):
        spotify_url = str(getattr(row, "spotify_url", "") or "").strip()
        youtube_url = str(getattr(row, "youtube_url", "") or "").strip()
        parts = [
            f"**{int(getattr(row, 'position')):02d}. {getattr(row, 'artist')} - {getattr(row, 'track')}**",
            f"`{getattr(row, 'duration_text')}`",
        ]
        link_parts: list[str] = []
        if spotify_url:
            link_parts.append(f"[Spotify]({spotify_url})")
        if youtube_url:
            link_parts.append(f"[YouTube]({youtube_url})")
        if link_parts:
            parts.append(" · ".join(link_parts))
        lines.append(" · ".join(parts))
    base_app.st.markdown("\n\n".join(lines))


def _render_player_section(queue: pd.DataFrame) -> None:
    base_app.st.subheader("Playable Playlist")
    _render_queue_table(queue)
    selected_position = base_app.st.slider("Now Playing", 1, int(len(queue)), 1)
    playback_platform = base_app.st.radio("Playback", options=["Auto", "Spotify", "YouTube"], horizontal=True)

    current_signature = f"{selected_position}:{playback_platform}"
    if base_app.st.session_state.get("lighter_player_signature") != current_signature:
        base_app.st.session_state["lighter_player_signature"] = current_signature
        if "lighter_player_open" not in base_app.st.session_state:
            base_app.st.session_state["lighter_player_open"] = False

    open_requested = base_app.st.button("Open Player", key="lighter_open_player_button")
    if open_requested:
        base_app.st.session_state["lighter_player_open"] = True

    if not base_app.st.session_state.get("lighter_player_open", False):
        base_app.st.caption("Player loads only when you open it.")
        return

    selected_row = queue.loc[queue["position"] == selected_position].iloc[0]
    selected_spotify = str(selected_row["spotify_url"]).strip()
    selected_youtube, selected_youtube_watch = base_app.resolve_playback_youtube_targets(selected_row)
    selected_youtube = str(selected_youtube).strip()
    selected_youtube_watch = str(selected_youtube_watch).strip()
    selected_spotify_track_id = base_app.extract_spotify_track_id(selected_spotify)

    if playback_platform == "YouTube" and not selected_youtube_watch:
        forced_link, forced_embed = base_app.resolve_playback_youtube_targets(
            selected_row,
            force_live=True,
            live_timeout=4,
        )
        selected_youtube = str(forced_link or selected_youtube).strip()
        selected_youtube_watch = str(forced_embed or selected_youtube_watch).strip()

    if playback_platform == "YouTube":
        if selected_youtube_watch:
            base_app.st.video(selected_youtube_watch)
        elif selected_spotify_track_id:
            base_app.st.info("YouTube embed unavailable for this track. Falling back to Spotify player.")
            _render_spotify_embed(selected_spotify_track_id)
        elif selected_youtube:
            base_app.st.info("No direct YouTube video ID available for embed. Use the YouTube link in the playlist.")
    elif playback_platform == "Spotify":
        if selected_spotify_track_id:
            _render_spotify_embed(selected_spotify_track_id)
        elif selected_spotify:
            base_app.st.info("No direct Spotify track ID available for embed. Use the Spotify link in the playlist.")
    else:
        if selected_youtube_watch:
            base_app.st.video(selected_youtube_watch)
        elif selected_spotify_track_id:
            _render_spotify_embed(selected_spotify_track_id)
        elif selected_youtube:
            base_app.st.info("Use the YouTube link in the playlist for this track.")


def _render_temp_playlist_section(queue: pd.DataFrame, queue_signature: str) -> None:
    with base_app.st.sidebar:
        base_app.st.markdown("---")
        build_requested = base_app.st.button(
            "Build Temporary YouTube Playlist",
            use_container_width=True,
            key="lighter_ui_build_temp_playlist",
        )

    if build_requested:
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
        base_app.st.session_state["lighter_ui_temp_playlist"] = {
            "signature": queue_signature,
            "youtube_ids": youtube_ids,
            "yt_stats": yt_stats,
        }

    stored = base_app.st.session_state.get("lighter_ui_temp_playlist")
    if not stored or stored.get("signature") != queue_signature:
        with base_app.st.sidebar:
            base_app.st.caption("Playlist link is generated only when you click the button.")
        return

    youtube_ids = stored.get("youtube_ids", [])
    yt_stats = stored.get("yt_stats", {})

    if len(youtube_ids) >= 2:
        temp_youtube_playlist = "https://www.youtube.com/watch_videos?video_ids=" + ",".join(youtube_ids[:50])
        with base_app.st.sidebar:
            base_app.render_platform_link("Open Temporary YouTube Playlist", temp_youtube_playlist)
    else:
        with base_app.st.sidebar:
            base_app.st.caption("Temporary YouTube playlist link requires at least 2 playable YouTube IDs.")

    target_rows = int(yt_stats.get("target_rows", 0) or 0)
    playable_rows = int(yt_stats.get("playable_count", 0) or 0)
    linked_rows = int(yt_stats.get("playable_linked_rows", playable_rows) or playable_rows)
    if target_rows > 0:
        coverage_pct = (linked_rows / target_rows) * 100.0
        duplicate_collapsed = max(0, linked_rows - playable_rows)
        with base_app.st.sidebar:
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


def main() -> None:
    patch_base_app_for_phase2_lighter()

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
        data = prepare_music_data_cached_phase2(raw_df)
    except Exception as exc:
        _render_hidden_startup_health("Unhealthy")
        base_app.st.error(str(exc))
        base_app.st.stop()

    if data.empty:
        _render_hidden_startup_health("Unhealthy")
        base_app.st.error("No usable rows found after cleaning the dataset.")
        base_app.st.stop()

    _render_hidden_startup_health("Healthy")

    base_app._init_seed_selection_state(data)
    song_options, artist_options, quick_top_songs, quick_top_artists = get_seed_ui_options_phase2(data)

    base_app.st.markdown('<div class="action-title">Choose your move</div>', unsafe_allow_html=True)
    if "lighter_surprise_counter" not in base_app.st.session_state:
        base_app.st.session_state["lighter_surprise_counter"] = 0
    if "lighter_surprise_seed_track" not in base_app.st.session_state:
        base_app.st.session_state["lighter_surprise_seed_track"] = ""

    with base_app.st.form("lighter_generate_form"):
        experience_mode = base_app.st.radio(
            "Mix Mode",
            options=["Quick Mode", "Self Mix"],
            key="lighter_experience_mode",
            horizontal=True,
            label_visibility="collapsed",
        )

        controls_col, mood_col = base_app.st.columns([1.3, 1.0], gap="large")

        with controls_col:
            if experience_mode == "Quick Mode":
                selected_quick_vibe = base_app.st.radio(
                    "Vibe Options",
                    options=list(base_app.PERSONALITY_PROFILES.keys()),
                    key="lighter_quick_vibe_option",
                    horizontal=True,
                )
                vibe_seed, vibe_description = base_app.personality_seed_option(data, selected_quick_vibe)
                base_app.st.caption(vibe_description)
                seed_weights = {str(vibe_seed): 1.0}
                experience_state = {"experience_mode": "Quick Mode", "quick_vibe": selected_quick_vibe}
            else:
                start_mode = base_app.st.radio(
                    "How to Start",
                    options=["Pick Songs", "Pick Artists", "Surprise Me"],
                    key="lighter_start_mode",
                    horizontal=True,
                    label_visibility="collapsed",
                )
                if start_mode == "Pick Songs":
                    base_app.st.session_state["selected_seed_songs"] = _render_seed_choice_checkboxes(
                        "Quick top songs",
                        quick_top_songs,
                        "selected_seed_songs",
                    )
                    base_app.st.session_state["selected_seed_artists"] = []
                elif start_mode == "Pick Artists":
                    base_app.st.session_state["selected_seed_artists"] = _render_seed_choice_checkboxes(
                        "Quick top artists",
                        quick_top_artists,
                        "selected_seed_artists",
                    )
                    base_app.st.session_state["selected_seed_songs"] = []
                else:
                    base_app.st.session_state["selected_seed_songs"] = []
                    base_app.st.session_state["selected_seed_artists"] = []
                    surprise_seed = base_app.st.session_state.get("lighter_surprise_seed_track") or base_app._default_seed_track(data)
                    base_app.st.caption("A random starter is chosen when you generate the playlist.")
                    base_app.st.success(f"Current random starter: {surprise_seed}")

                seed_weights = base_app._build_seed_weights_from_state(data)
                if start_mode == "Surprise Me":
                    current_surprise = base_app.st.session_state.get("lighter_surprise_seed_track") or base_app._default_seed_track(data)
                    seed_weights = {str(current_surprise): 1.0}
                if not seed_weights:
                    seed_weights = {base_app._default_seed_track(data): 1.0}
                experience_state = {"experience_mode": "Self Mix", "start_mode": start_mode}

        with mood_col:
            base_app.st.markdown("#### Recommendation Controls")
            if experience_state.get("experience_mode") == "Quick Mode":
                quick_defaults = base_app.QUICK_VIBE_DEFAULTS.get(
                    str(experience_state.get("quick_vibe", "Focus Flow")),
                    base_app.QUICK_VIBE_DEFAULTS["Focus Flow"],
                )
                if base_app.st.session_state.get("quick_vibe_applied") != experience_state.get("quick_vibe"):
                    base_app.st.session_state["quick_spotify_weight_pct"] = int(quick_defaults["spotify_weight_pct"])
                    base_app.st.session_state["quick_discovery_hits_pct"] = int(quick_defaults["discovery_hits_pct"])
                    base_app.st.session_state["quick_vibe_applied"] = experience_state.get("quick_vibe")

                mood = str(quick_defaults["mood"])
                spotify_weight_pct = base_app.st.slider("Platform Bias", 0, 100, key="quick_spotify_weight_pct")
                youtube_weight_pct = 100 - spotify_weight_pct
                base_app.st.caption(f"Spotify ({spotify_weight_pct}%) <- -> YouTube ({youtube_weight_pct}%)")
                discovery_hits_pct = base_app.st.slider("Discovery Mode", 0, 100, key="quick_discovery_hits_pct")
                hidden_gems_pct = 100 - discovery_hits_pct
                base_app.st.caption(f"Hits ({discovery_hits_pct}%) <- -> Hidden Gems ({hidden_gems_pct}%)")
            else:
                self_mix_vibe = base_app.st.radio(
                    "Vibe Options",
                    options=list(base_app.PERSONALITY_PROFILES.keys()),
                    index=0,
                    key="lighter_self_mix_vibe_option",
                    horizontal=True,
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

        generate_clicked = base_app.st.form_submit_button(
            "Generate Playlist",
            use_container_width=True,
        )

    seed_weight_items = tuple(sorted((str(name), float(weight)) for name, weight in seed_weights.items()))
    preferred_artist_weight_items: tuple[tuple[str, float], ...] = ()
    if experience_state.get("experience_mode") == "Self Mix":
        preferred_artist_weight_items = tuple(sorted(base_app._build_preferred_artist_weights(data).items()))
    include_seed_tracks = discovery_hits_pct == 100

    if generate_clicked and experience_state.get("experience_mode") == "Self Mix" and experience_state.get("start_mode") == "Surprise Me":
        base_app.st.session_state["lighter_surprise_counter"] += 1
        base_app.st.session_state["lighter_surprise_seed_track"] = _pick_surprise_seed(
            data,
            base_app.st.session_state["lighter_surprise_counter"],
        )
        seed_weights = {str(base_app.st.session_state["lighter_surprise_seed_track"]): 1.0}
        seed_weight_items = tuple(sorted((str(name), float(weight)) for name, weight in seed_weights.items()))

    generation_signature = _generation_signature(
        seed_weight_items=seed_weight_items,
        mood=mood,
        spotify_weight_pct=spotify_weight_pct,
        discovery_hits_pct=discovery_hits_pct,
        target_minutes=target_minutes,
        preferred_artist_weight_items=preferred_artist_weight_items,
        include_seed_tracks=include_seed_tracks,
    )

    if generate_clicked:
        base_app.st.session_state["lighter_generated_signature"] = generation_signature

    if base_app.st.session_state.get("lighter_generated_signature") != generation_signature:
        base_app.st.caption("Choose your inputs, then generate the playlist.")
        base_app.st.stop()

    recommendations = recommend_tracks_phase2(
        data=data,
        seed_weight_items=seed_weight_items,
        mood=mood,
        spotify_weight=spotify_weight_pct / 100.0,
        discovery_mode=hidden_gems_pct / 100.0,
        top_k=180,
        exclude_seed_tracks=not include_seed_tracks,
        preferred_artist_weight_items=preferred_artist_weight_items,
    )
    playlist = build_duration_playlist_phase2(
        recommendations=recommendations,
        target_minutes=target_minutes,
        tolerance_minutes=base_app.PLAYLIST_TOLERANCE_MINUTES,
        candidate_limit=180,
        max_tracks=50,
    )
    playlist["mood_tag"] = mood

    if playlist.empty:
        base_app.st.warning("No playlist could be generated for this duration target. Try different seed selections.")
        base_app.st.stop()

    queue = playlist.copy().reset_index(drop=True)
    queue["position"] = queue.index + 1
    queue["duration_text"] = queue["duration_ms"].apply(base_app.format_track_duration)
    queue["spotify_url"] = queue.apply(
        lambda row: row.get("spotify_link") or row.get("url_spotify") or "",
        axis=1,
    )
    queue["youtube_url"] = queue.apply(
        lambda row: base_app.prefer_direct_youtube_url(row.get("youtube_link"), row.get("url_youtube")),
        axis=1,
    )
    queue["queue_label"] = queue.apply(
        lambda row: f'{int(row["position"]):02d}. {row["artist"]} - {row["track"]} ({row["duration_text"]})',
        axis=1,
    )
    queue_signature = _queue_signature(queue, mood, target_minutes)
    _render_player_section(queue)
    _render_temp_playlist_section(queue, queue_signature)


if __name__ == "__main__":
    main()
