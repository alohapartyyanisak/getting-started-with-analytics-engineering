from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _offline_v2_code_dir() -> Path:
    return _repo_root() / "recommendation_app" / "offline-v2" / "code"


def _phase_cache_dir() -> Path:
    path = _repo_root() / "recommendation_app" / "go-live" / ".cache" / "phase_checks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_prepared_cache(cache_key: str) -> pd.DataFrame | None:
    cache_dir = _phase_cache_dir()
    parquet_path = cache_dir / f"prepared_{cache_key}.parquet"
    pickle_path = cache_dir / f"prepared_{cache_key}.pkl"
    if parquet_path.exists():
        try:
            frame = pd.read_parquet(parquet_path)
            if not frame.empty:
                return frame
        except Exception:
            pass
    if pickle_path.exists():
        try:
            frame = pd.read_pickle(pickle_path)
            if not frame.empty:
                return frame
        except Exception:
            pass
    return None


def _save_prepared_cache(cache_key: str, frame: pd.DataFrame) -> None:
    cache_dir = _phase_cache_dir()
    parquet_path = cache_dir / f"prepared_{cache_key}.parquet"
    pickle_path = cache_dir / f"prepared_{cache_key}.pkl"
    try:
        frame.to_parquet(parquet_path, index=False)
        return
    except Exception:
        pass
    try:
        frame.to_pickle(pickle_path)
    except Exception:
        return


def _load_from_phase2_managed() -> tuple[pd.DataFrame, dict[str, Any]] | None:
    try:
        from phase2_managed_loader import load_prepared_dataset, release_summary
    except Exception:
        return None

    try:
        prepared, release = load_prepared_dataset()
        if prepared.empty:
            return None
        summary = release_summary(release)
        return prepared, {
            "dataset_source": "phase2_managed",
            "dataset_version": release.version,
            "dataset_summary": summary,
        }
    except Exception:
        return None


def load_prepared_dataset_for_phase_checks(prefer_managed: bool = True) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Shared loader used by phase checks.
    Priority:
    1) Managed Phase 2 prepared snapshot (if available)
    2) Cached prepared frame keyed by Kaggle source fingerprint
    3) Fresh load_and_merge + prepare, then cache
    """
    code_dir = _offline_v2_code_dir()
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))

    if prefer_managed:
        managed = _load_from_phase2_managed()
        if managed is not None:
            return managed

    import data_upgrade_v2  # type: ignore
    import recommender_v2_adapter  # type: ignore

    try:
        baseline_path = data_upgrade_v2._resolve_cached_dataset_path(data_upgrade_v2.BASELINE_DATASET_ID)
        expansion_path = data_upgrade_v2._resolve_cached_dataset_path(data_upgrade_v2.EXPANSION_DATASET_ID)
        baseline_csvs = data_upgrade_v2._resolve_dataset_csv(baseline_path, data_upgrade_v2.BASELINE_DATASET_ID)
        expansion_csvs = data_upgrade_v2._resolve_dataset_csv(expansion_path, data_upgrade_v2.EXPANSION_DATASET_ID)
        cache_key = data_upgrade_v2._source_fingerprint([baseline_csvs[0], *expansion_csvs])
    except Exception:
        cache_key = "fallback"

    cached = _load_prepared_cache(cache_key)
    if cached is not None:
        return cached, {
            "dataset_source": "prepared_cache",
            "cache_key": cache_key,
        }

    raw_df, source_path, dataset_path = data_upgrade_v2.load_and_merge_from_kagglehub()
    prepared = recommender_v2_adapter.prepare_music_data(raw_df)
    if prepared.empty:
        raise ValueError("Prepared dataset is empty in phase shared loader.")
    _save_prepared_cache(cache_key, prepared)
    return prepared, {
        "dataset_source": "kaggle_merge",
        "cache_key": cache_key,
        "source_path": source_path,
        "dataset_path": dataset_path,
        "rows_raw": int(len(raw_df)),
    }

