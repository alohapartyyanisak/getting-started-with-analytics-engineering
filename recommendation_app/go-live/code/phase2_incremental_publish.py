from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import phase2_runtime_config as cfg


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _schema_hash(df: pd.DataFrame) -> str:
    schema = [{"name": str(col), "dtype": str(dtype)} for col, dtype in zip(df.columns, df.dtypes)]
    encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _to_float(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0.0)


def _minmax(series: pd.Series) -> pd.Series:
    values = _to_float(series)
    minimum = float(values.min()) if len(values) else 0.0
    maximum = float(values.max()) if len(values) else 0.0
    if not np.isfinite(minimum) or not np.isfinite(maximum) or maximum <= minimum:
        return pd.Series(np.zeros(len(values), dtype=float), index=values.index)
    return (values - minimum) / (maximum - minimum)


def _recompute_global_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for required in ("views", "likes", "comments", "stream", "tempo"):
        if required not in out.columns:
            out[required] = 0.0
        out[required] = _to_float(out[required])

    safe_views = out["views"].replace(0, np.nan)
    out["engagement_rate"] = ((out["likes"] + out["comments"]) / safe_views).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    out["stream_to_view_ratio"] = (out["stream"] / safe_views).replace([np.inf, -np.inf], np.nan).fillna(0.0)

    out["views_norm"] = _minmax(np.log1p(out["views"]))
    out["stream_norm"] = _minmax(np.log1p(out["stream"]))
    out["engagement_norm"] = _minmax(out["engagement_rate"])
    out["ratio_norm"] = _minmax(out["stream_to_view_ratio"])
    out["tempo_scaled"] = _minmax(out["tempo"])
    out["popularity_norm"] = _minmax(np.log1p(out["views"] + out["stream"]))
    out["momentum_score"] = (
        _to_float(out["views_norm"]) + _to_float(out["engagement_norm"]) + _to_float(out["ratio_norm"])
    ) / 3.0
    return out


def _row_digest(df: pd.DataFrame, columns: list[str]) -> pd.Series:
    stable = df[columns].copy()
    for column in columns:
        stable[column] = stable[column].where(~stable[column].isna(), "")
    # Hashing as strings keeps deterministic behavior across mixed dtypes.
    return pd.util.hash_pandas_object(stable.astype(str), index=False).astype(str)


def _choose_compare_columns(raw: pd.DataFrame, current: pd.DataFrame) -> list[str]:
    excluded = {
        "engagement_rate",
        "stream_to_view_ratio",
        "views_norm",
        "stream_norm",
        "engagement_norm",
        "ratio_norm",
        "tempo_scaled",
        "popularity_norm",
        "momentum_score",
        "recommendation_score",
        "similarity_score",
        "platform_score",
        "discovery_score",
        "link_quality_score",
        "youtube_quality_score",
    }
    candidates = sorted((set(raw.columns) & set(current.columns)) - excluded)
    if "canonical_key" not in candidates:
        raise ValueError("canonical_key missing from compare columns; cannot run incremental upsert.")
    return candidates


def _provider_from_uri(uri: str) -> str:
    text = str(uri).strip().lower()
    if text.startswith("s3://"):
        return "s3"
    if text.startswith("gs://"):
        return "gcs"
    if text.startswith("abfs://") or text.startswith("az://") or text.startswith("wasbs://"):
        return "azure_blob"
    return "unknown"


def _upload_bytes(uri: str, payload: bytes) -> None:
    try:
        import fsspec  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Cloud upload requested but fsspec is not available. Install provider packages "
            "(for example s3fs/gcsfs/adlfs)."
        ) from exc

    with fsspec.open(uri, "wb") as handle:
        handle.write(payload)


def _upload_file(local_path: Path, uri: str) -> dict[str, Any]:
    try:
        import fsspec  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Cloud upload requested but fsspec is not available. Install provider packages "
            "(for example s3fs/gcsfs/adlfs)."
        ) from exc

    with local_path.open("rb") as src:
        with fsspec.open(uri, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return {
        "uri": uri,
        "sha256": _sha256_file(local_path),
        "size_bytes": int(local_path.stat().st_size),
    }


def _source_fingerprint(data_upgrade_v2: Any) -> tuple[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for dataset_id in (data_upgrade_v2.BASELINE_DATASET_ID, data_upgrade_v2.EXPANSION_DATASET_ID):
        dataset_path = data_upgrade_v2._resolve_cached_dataset_path(dataset_id)  # type: ignore[attr-defined]
        csvs = data_upgrade_v2._resolve_dataset_csv(dataset_path, dataset_id)  # type: ignore[attr-defined]
        for csv_path in csvs:
            stat = csv_path.stat()
            rows.append(
                {
                    "dataset_id": dataset_id,
                    "file_name": csv_path.name,
                    "path": str(csv_path),
                    "size_bytes": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                }
            )
    rows = sorted(rows, key=lambda item: (item["dataset_id"], item["file_name"], item["size_bytes"], item["mtime_ns"]))
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return _sha256_bytes(encoded), rows


def incremental_publish(force: bool = False) -> dict[str, Any]:
    offline_code_dir = cfg.OFFLINE_V2_CODE_DIR
    if str(offline_code_dir) not in sys.path:
        sys.path.insert(0, str(offline_code_dir))

    import data_upgrade_v2  # type: ignore
    import recommender_v2_adapter  # type: ignore

    import phase2_managed_loader as managed_loader

    dataset_root = cfg.dataset_root()
    dataset_root.mkdir(parents=True, exist_ok=True)
    pointer_path = dataset_root / cfg.DATASET_POINTER_FILE

    source_fp, source_files = _source_fingerprint(data_upgrade_v2)
    previous_release = None
    previous_metadata: dict[str, Any] = {}
    current_df = pd.DataFrame()

    try:
        current_df, previous_release = managed_loader.load_prepared_dataset(dataset_root=dataset_root)
        previous_metadata = _read_json(previous_release.metadata_path)
    except Exception:
        # Bootstrap path.
        current_df = pd.DataFrame()
        previous_release = None
        previous_metadata = {}

    previous_source_fp = (
        str(previous_metadata.get("source", {}).get("source_fingerprint", "")).strip()
        if isinstance(previous_metadata.get("source", {}), dict)
        else ""
    )
    if previous_release is not None and previous_source_fp and previous_source_fp == source_fp and not force:
        return {
            "status": "noop",
            "reason": "NOOP_NO_SOURCE_CHANGE",
            "dataset_version": previous_release.version,
            "row_count": int(len(current_df)),
            "source_fingerprint": source_fp,
            "source_files": source_files,
        }

    raw_df, source_path, dataset_path = data_upgrade_v2.load_and_merge_from_kagglehub(
        baseline_dataset_id=data_upgrade_v2.BASELINE_DATASET_ID,
        expansion_dataset_id=data_upgrade_v2.EXPANSION_DATASET_ID,
    )
    if "canonical_key" not in raw_df.columns:
        raise ValueError("Raw merged frame missing canonical_key; cannot run incremental upsert.")

    # Keep deterministic single row per canonical key for change detection/upsert.
    rank_cols = [col for col in ("views", "stream", "likes", "comments") if col in raw_df.columns]
    if rank_cols:
        raw_keyed = raw_df.sort_values(rank_cols, ascending=[False] * len(rank_cols))
    else:
        raw_keyed = raw_df.copy()
    raw_keyed = raw_keyed.drop_duplicates(subset=["canonical_key"], keep="first").reset_index(drop=True)

    if current_df.empty:
        delta_raw = raw_keyed.copy()
        insert_keys = set(delta_raw["canonical_key"].astype(str))
        update_keys: set[str] = set()
        unchanged_count = 0
        compare_columns: list[str] = sorted(raw_keyed.columns.tolist())
    else:
        current_keyed = current_df.drop_duplicates(subset=["canonical_key"], keep="first").reset_index(drop=True)
        compare_columns = _choose_compare_columns(raw_keyed, current_keyed)

        raw_digest = pd.DataFrame(
            {
                "canonical_key": raw_keyed["canonical_key"].astype(str),
                "raw_digest": _row_digest(raw_keyed, compare_columns),
            }
        )
        cur_digest = pd.DataFrame(
            {
                "canonical_key": current_keyed["canonical_key"].astype(str),
                "cur_digest": _row_digest(current_keyed, compare_columns),
            }
        )
        merged_digest = raw_digest.merge(cur_digest, on="canonical_key", how="left")

        insert_keys = set(merged_digest.loc[merged_digest["cur_digest"].isna(), "canonical_key"].tolist())
        update_keys = set(
            merged_digest.loc[
                merged_digest["cur_digest"].notna() & (merged_digest["raw_digest"] != merged_digest["cur_digest"]),
                "canonical_key",
            ].tolist()
        )
        unchanged_count = int(len(merged_digest) - len(insert_keys) - len(update_keys))
        delta_keys = insert_keys | update_keys
        delta_raw = raw_keyed[raw_keyed["canonical_key"].astype(str).isin(delta_keys)].reset_index(drop=True)

    if delta_raw.empty and not force:
        return {
            "status": "noop",
            "reason": "NOOP_NO_DATA_CHANGE",
            "dataset_version": previous_release.version if previous_release else "",
            "row_count": int(len(current_df)),
            "source_fingerprint": source_fp,
            "source_files": source_files,
        }

    delta_prepared = recommender_v2_adapter.prepare_music_data(delta_raw)
    if delta_prepared.empty:
        raise ValueError("Delta prepared dataset is empty; incremental publish aborted.")

    if current_df.empty:
        combined = delta_prepared.copy()
    else:
        keep_current = current_df[~current_df["canonical_key"].astype(str).isin(update_keys)].copy()
        combined = pd.concat([keep_current, delta_prepared], axis=0, ignore_index=True, sort=False)

    combined = combined.drop_duplicates(subset=["canonical_key"], keep="last").reset_index(drop=True)
    combined = _recompute_global_features(combined)

    now = datetime.now(timezone.utc)
    version = now.strftime("ds_%Y%m%d_%H%M%S_utc")
    snapshot_dir = dataset_root / "snapshots" / version
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    artifact_paths: dict[str, Path] = {}
    if cfg.PUBLISH_INCLUDE_PARQUET:
        parquet_path = snapshot_dir / "prepared.parquet"
        try:
            combined.to_parquet(parquet_path, index=False)
            artifact_paths["parquet"] = parquet_path
        except Exception:
            pass
    if cfg.PUBLISH_INCLUDE_CSV:
        csv_path = snapshot_dir / "prepared.csv"
        combined.to_csv(csv_path, index=False)
        artifact_paths["csv"] = csv_path
    if not artifact_paths:
        raise ValueError("At least one artifact format must be enabled (CSV or Parquet).")

    preferred_local = artifact_paths.get("parquet") or artifact_paths.get("csv")
    assert preferred_local is not None
    prepared_uri = cfg.storage_ref(preferred_local, dataset_root)

    cloud_root = cfg.PRIMARY_ARTIFACT_ROOT_URI
    cloud_uploads: dict[str, dict[str, Any]] = {}
    cloud_metadata_uri = ""
    cloud_pointer_uri = ""
    if cloud_root:
        root = cloud_root.rstrip("/")
        for name, local_path in artifact_paths.items():
            target_uri = f"{root}/snapshots/{version}/{local_path.name}"
            cloud_uploads[name] = _upload_file(local_path, target_uri)
        preferred_remote = cloud_uploads.get("parquet") or cloud_uploads.get("csv")
        if preferred_remote:
            prepared_uri = str(preferred_remote.get("uri", prepared_uri))

    metadata = {
        "dataset_version": version,
        "created_at_utc": now.isoformat(),
        "row_count": int(len(combined)),
        "schema_hash_sha256": _schema_hash(combined),
        "prepared_uri": prepared_uri,
        "artifacts": {
            name: {
                "path": cfg.storage_ref(path, dataset_root),
                "sha256": _sha256_file(path),
                "size_bytes": int(path.stat().st_size),
            }
            for name, path in artifact_paths.items()
        },
        "cloud_artifacts": cloud_uploads,
        "source": {
            "baseline_dataset_id": data_upgrade_v2.BASELINE_DATASET_ID,
            "expansion_dataset_id": data_upgrade_v2.EXPANSION_DATASET_ID,
            "source_path": source_path,
            "dataset_path": dataset_path,
            "rows_raw": int(len(raw_df)),
            "source_fingerprint": source_fp,
            "source_files": source_files,
        },
        "incremental": {
            "publish_mode": "incremental_upsert",
            "parent_dataset_version": previous_release.version if previous_release else "",
            "delta_insert_count": int(len(insert_keys)),
            "delta_update_count": int(len(update_keys)),
            "delta_unchanged_count": int(unchanged_count),
            "compare_columns": compare_columns,
        },
    }

    metadata_path = snapshot_dir / "metadata.json"
    metadata_bytes = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
    metadata_path.write_bytes(metadata_bytes)

    latest_payload = {
        "dataset_version": version,
        "updated_at_utc": now.isoformat(),
        "metadata_uri": cfg.storage_ref(metadata_path, dataset_root),
    }
    pointer_bytes = json.dumps(latest_payload, ensure_ascii=False, indent=2).encode("utf-8")
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_bytes(pointer_bytes)

    if cloud_root:
        root = cloud_root.rstrip("/")
        cloud_metadata_uri = f"{root}/snapshots/{version}/metadata.json"
        cloud_pointer_uri = f"{root}/{cfg.DATASET_POINTER_FILE}"
        _upload_bytes(cloud_metadata_uri, metadata_bytes)
        _upload_bytes(cloud_pointer_uri, pointer_bytes)

    changelog = pd.DataFrame(
        [
            {"canonical_key": key, "change_type": "insert"}
            for key in sorted(insert_keys)
        ]
        + [
            {"canonical_key": key, "change_type": "update"}
            for key in sorted(update_keys)
        ]
    )
    changelog_path = snapshot_dir / "delta_changes.csv"
    changelog.to_csv(changelog_path, index=False)

    return {
        "status": "ok",
        "publish_mode": "incremental_upsert",
        "dataset_root": str(dataset_root.resolve()),
        "dataset_version": version,
        "parent_dataset_version": previous_release.version if previous_release else "",
        "pointer_path": str(pointer_path.resolve()),
        "metadata_path": str(metadata_path.resolve()),
        "artifacts": {name: str(path.resolve()) for name, path in artifact_paths.items()},
        "cloud_metadata_uri": cloud_metadata_uri,
        "cloud_pointer_uri": cloud_pointer_uri,
        "row_count": int(len(combined)),
        "delta_insert_count": int(len(insert_keys)),
        "delta_update_count": int(len(update_keys)),
        "delta_unchanged_count": int(unchanged_count),
        "source_fingerprint": source_fp,
        "provider_hint": _provider_from_uri(cloud_root) if cloud_root else "local",
    }


if __name__ == "__main__":
    force = str(sys.argv[1]).strip().lower() == "--force" if len(sys.argv) > 1 else False
    print(json.dumps(incremental_publish(force=force), ensure_ascii=False, indent=2))
