from __future__ import annotations

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
_ORIGINAL_MAIN = base_app.main
_ORIGINAL_BUILD_PLAYABLE_YOUTUBE_IDS = base_app.build_playable_youtube_ids
_ORIGINAL_RENDER_MODERN_DEBUG_TABLE = base_app.render_modern_debug_table
_ORIGINAL_ST_VIDEO = base_app.st.video
_ORIGINAL_COMPONENTS_HTML = base_app.components.html
_ORIGINAL_RENDER_PLATFORM_LINK = base_app.render_platform_link


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
    # Phase 2 runtime reads precomputed prepared dataset artifacts.
    try:
        return validate_prepared_dataset(raw_df.copy())
    except Exception:
        # Safety fallback: if a raw frame is loaded by mistake, keep app operable.
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


def _current_render_count() -> int:
    return int(base_app.st.session_state.get("_go_live_render_count", 0) or 0)


def _defer_heavy_rendering() -> bool:
    return _current_render_count() <= 1


def _reset_dev_smoke_markers() -> None:
    base_app.st.session_state["_go_live_dev_result_surface_ready"] = False
    base_app.st.session_state["_go_live_dev_player_surface_ready"] = False
    base_app.st.session_state["_go_live_dev_temp_playlist_ready"] = False
    base_app.st.session_state["_go_live_dev_debug_table_ready"] = False


def _render_hidden_phase2_markers() -> None:
    startup_health = str(base_app.st.session_state.get("startup_health_status", "Unknown") or "Unknown").strip()
    final_pick_count = (
        len(base_app.st.session_state.get("selected_seed_artists", []))
        + len(base_app.st.session_state.get("selected_seed_songs", []))
    )
    result_surface_ready = str(
        bool(base_app.st.session_state.get("_go_live_dev_result_surface_ready", False))
    ).lower()
    player_surface_ready = str(
        bool(base_app.st.session_state.get("_go_live_dev_player_surface_ready", False))
    ).lower()
    temp_playlist_ready = str(
        bool(base_app.st.session_state.get("_go_live_dev_temp_playlist_ready", False))
    ).lower()
    debug_table_ready = str(
        bool(base_app.st.session_state.get("_go_live_dev_debug_table_ready", False))
    ).lower()
    base_app.st.markdown(
        (
            '<div data-testid="startup-health-status" style="display:none">{startup}</div>'
            '<div data-testid="dev-final-picks-count" style="display:none">{final_picks}</div>'
            '<div data-testid="dev-result-surface-ready" style="display:none">{result_ready}</div>'
            '<div data-testid="dev-player-surface-ready" style="display:none">{player_ready}</div>'
            '<div data-testid="dev-temp-playlist-ready" style="display:none">{temp_ready}</div>'
            '<div data-testid="dev-debug-table-ready" style="display:none">{debug_ready}</div>'
        ).format(
            startup=startup_health,
            final_picks=final_pick_count,
            result_ready=result_surface_ready,
            player_ready=player_surface_ready,
            temp_ready=temp_playlist_ready,
            debug_ready=debug_table_ready,
        ),
        unsafe_allow_html=True,
    )


def build_playable_youtube_ids_phase2(*args: Any, **kwargs: Any):
    if _defer_heavy_rendering():
        queue = args[0] if args else kwargs.get("queue")
        target_rows = min(len(queue), int(kwargs.get("max_ids", 50) or 50)) if isinstance(queue, pd.DataFrame) else 0
        return (
            [],
            {
                "playable_count": 0,
                "playable_linked_rows": 0,
                "target_rows": target_rows,
                "included_direct": 0,
                "included_resolved": 0,
                "resolver_attempted": 0,
                "resolver_resolved": 0,
                "duplicate_rows": 0,
                "unresolved_rows": target_rows,
                "budget_blocked_rows": 0,
                "resolve_budget": int(kwargs.get("max_live_resolves", 0) or 0),
                "row_diagnostics": [],
            },
        )
    return _ORIGINAL_BUILD_PLAYABLE_YOUTUBE_IDS(*args, **kwargs)


def render_modern_debug_table_phase2(playlist: pd.DataFrame) -> None:
    if not playlist.empty:
        base_app.st.session_state["_go_live_dev_debug_table_ready"] = True
        base_app.st.session_state["_go_live_dev_result_surface_ready"] = True
    if _defer_heavy_rendering():
        base_app.st.caption("Recommendation Debug View loads after the first interaction.")
        return
    _ORIGINAL_RENDER_MODERN_DEBUG_TABLE(playlist)


def st_video_phase2(*args: Any, **kwargs: Any):
    base_app.st.session_state["_go_live_dev_player_surface_ready"] = True
    base_app.st.session_state["_go_live_dev_result_surface_ready"] = True
    if _defer_heavy_rendering():
        base_app.st.caption("Playback embed loads after the first interaction.")
        return None
    return _ORIGINAL_ST_VIDEO(*args, **kwargs)


def components_html_phase2(*args: Any, **kwargs: Any):
    base_app.st.session_state["_go_live_dev_player_surface_ready"] = True
    base_app.st.session_state["_go_live_dev_result_surface_ready"] = True
    if _defer_heavy_rendering():
        return None
    return _ORIGINAL_COMPONENTS_HTML(*args, **kwargs)


def render_platform_link_phase2(label: str, url: str) -> None:
    text = str(label or "").strip()
    if text:
        base_app.st.session_state["_go_live_dev_result_surface_ready"] = True
    if text in {"Spotify", "YouTube"}:
        base_app.st.session_state["_go_live_dev_player_surface_ready"] = True
    if text == "Open Temporary YouTube Playlist":
        base_app.st.session_state["_go_live_dev_temp_playlist_ready"] = True
    _ORIGINAL_RENDER_PLATFORM_LINK(label, url)


def main_phase2() -> None:
    _reset_dev_smoke_markers()
    base_app.st.session_state["_go_live_render_count"] = _current_render_count() + 1
    _ORIGINAL_MAIN()
    _render_hidden_phase2_markers()


def patch_base_app_for_phase2() -> None:
    app_v2.patch_base_app_for_v2()
    base_app.DEFAULT_DATASET_ID = "managed://latest"
    base_app.read_source = read_source_phase2
    base_app.prepare_music_data_cached = prepare_music_data_cached_phase2
    # Avoid hashing full DataFrames in Streamlit cache on Cloud Run.
    base_app.recommend_tracks_cached = recommend_tracks_phase2
    base_app.build_duration_playlist_cached = build_duration_playlist_phase2
    base_app.get_seed_ui_options = get_seed_ui_options_phase2
    base_app.build_playable_youtube_ids = build_playable_youtube_ids_phase2
    base_app.render_modern_debug_table = render_modern_debug_table_phase2
    base_app.render_platform_link = render_platform_link_phase2
    base_app.st.video = st_video_phase2
    base_app.components.html = components_html_phase2
    base_app.main = main_phase2


if __name__ == "__main__":
    patch_base_app_for_phase2()
    base_app.main()
