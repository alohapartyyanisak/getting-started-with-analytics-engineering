from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import phase2_runtime_config as cfg


def _schema_hash(df: pd.DataFrame) -> str:
    schema = [{"name": str(col), "dtype": str(dtype)} for col, dtype in zip(df.columns, df.dtypes)]
    encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def publish_dataset() -> dict[str, Any]:
    offline_code_dir = cfg.OFFLINE_V2_CODE_DIR
    if str(offline_code_dir) not in sys.path:
        sys.path.insert(0, str(offline_code_dir))

    import data_upgrade_v2  # type: ignore
    import recommender_v2_adapter  # type: ignore

    now = datetime.now(timezone.utc)
    version = now.strftime("ds_%Y%m%d_%H%M%S_utc")
    dataset_root = cfg.dataset_root()
    snapshot_dir = dataset_root / "snapshots" / version
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    raw_df, source_path, dataset_path = data_upgrade_v2.load_and_merge_from_kagglehub(
        baseline_dataset_id=data_upgrade_v2.BASELINE_DATASET_ID,
        expansion_dataset_id=data_upgrade_v2.EXPANSION_DATASET_ID,
    )
    prepared_df = recommender_v2_adapter.prepare_music_data(raw_df)
    if prepared_df.empty:
        raise ValueError("Prepared dataset is empty; publish aborted.")

    artifact_paths: dict[str, Path] = {}
    if cfg.PUBLISH_INCLUDE_PARQUET:
        parquet_path = snapshot_dir / "prepared.parquet"
        try:
            prepared_df.to_parquet(parquet_path, index=False)
            artifact_paths["parquet"] = parquet_path
        except Exception:
            # Keep publish operable in runtimes without parquet engine.
            pass
    if cfg.PUBLISH_INCLUDE_CSV:
        csv_path = snapshot_dir / "prepared.csv"
        prepared_df.to_csv(csv_path, index=False)
        artifact_paths["csv"] = csv_path
    if not artifact_paths:
        raise ValueError("At least one artifact format must be enabled (CSV or Parquet).")

    preferred_artifact = artifact_paths.get("parquet") or artifact_paths.get("csv")
    assert preferred_artifact is not None

    metadata = {
        "dataset_version": version,
        "created_at_utc": now.isoformat(),
        "row_count": int(len(prepared_df)),
        "schema_hash_sha256": _schema_hash(prepared_df),
        "prepared_uri": cfg.storage_ref(preferred_artifact, dataset_root),
        "artifacts": {
            name: {
                "path": cfg.storage_ref(path, dataset_root),
                "sha256": _sha256_file(path),
                "size_bytes": int(path.stat().st_size),
            }
            for name, path in artifact_paths.items()
        },
        "source": {
            "baseline_dataset_id": data_upgrade_v2.BASELINE_DATASET_ID,
            "expansion_dataset_id": data_upgrade_v2.EXPANSION_DATASET_ID,
            "source_path": source_path,
            "dataset_path": dataset_path,
            "rows_raw": int(len(raw_df)),
        },
    }

    metadata_path = snapshot_dir / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    latest_payload = {
        "dataset_version": version,
        "updated_at_utc": now.isoformat(),
        "metadata_uri": cfg.storage_ref(metadata_path, dataset_root),
    }
    pointer_path = dataset_root / cfg.DATASET_POINTER_FILE
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(json.dumps(latest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "status": "ok",
        "dataset_root": str(dataset_root.resolve()),
        "dataset_version": version,
        "pointer_path": str(pointer_path.resolve()),
        "metadata_path": str(metadata_path.resolve()),
        "artifacts": {name: str(path.resolve()) for name, path in artifact_paths.items()},
        "row_count": int(len(prepared_df)),
    }


if __name__ == "__main__":
    print(json.dumps(publish_dataset(), ensure_ascii=False, indent=2))
