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


def patch_base_app_for_phase2() -> None:
    app_v2.patch_base_app_for_v2()
    base_app.DEFAULT_DATASET_ID = "managed://latest"
    base_app.read_source = read_source_phase2
    base_app.prepare_music_data_cached = prepare_music_data_cached_phase2


if __name__ == "__main__":
    patch_base_app_for_phase2()
    base_app.main()
