from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd

import phase2_runtime_config as cfg
from phase2_managed_loader import load_prepared_dataset, validate_prepared_dataset


if str(cfg.OFFLINE_V2_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(cfg.OFFLINE_V2_CODE_DIR))

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


def patch_base_app_for_phase2() -> None:
    app_v2.patch_base_app_for_v2()
    base_app.DEFAULT_DATASET_ID = "managed://latest"
    base_app.read_source = read_source_phase2
    base_app.prepare_music_data_cached = prepare_music_data_cached_phase2
    # Avoid hashing full DataFrames in Streamlit cache on Cloud Run.
    base_app.recommend_tracks_cached = recommend_tracks_phase2
    base_app.build_duration_playlist_cached = build_duration_playlist_phase2
    base_app.get_seed_ui_options = get_seed_ui_options_phase2


if __name__ == "__main__":
    patch_base_app_for_phase2()
    base_app.main()
