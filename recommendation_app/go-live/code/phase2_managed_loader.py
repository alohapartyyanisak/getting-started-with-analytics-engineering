from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd

import phase2_runtime_config as cfg

try:
    from prod.langsmith_app_tracing import emit_dataset_load_trace
except Exception:  # pragma: no cover
    emit_dataset_load_trace = None


REQUIRED_PREPARED_COLUMNS = {
    "artist",
    "track",
    "display_name",
    "duration_ms",
    "spotify_link",
    "youtube_link",
    "momentum_score",
    "popularity_norm",
    "danceability",
    "energy",
    "acousticness",
    "instrumentalness",
    "liveness",
    "valence",
    "speechiness",
    "tempo_scaled",
    "stream_norm",
    "views_norm",
    "stream",
    "views",
}


@dataclass
class DatasetRelease:
    version: str
    created_at_utc: str
    row_count: int
    schema_hash_sha256: str
    prepared_uri: str
    metadata_path: Path


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
    payload = json.dumps(schema, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_remote_uri(uri: str) -> bool:
    parsed = urlparse(str(uri or "").strip())
    return parsed.scheme.lower() in {"s3", "gs", "abfs", "az", "wasbs", "https", "http"}


def _sha256_uri(uri: str, chunk_size: int = 1024 * 1024) -> str:
    try:
        import fsspec  # type: ignore
    except Exception:
        return "unavailable"
    digest = hashlib.sha256()
    with fsspec.open(uri, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def pointer_path(dataset_root: Path | None = None) -> Path:
    root = dataset_root or cfg.dataset_root()
    return root / cfg.DATASET_POINTER_FILE


def resolve_release(version: str | None = None, dataset_root: Path | None = None) -> DatasetRelease:
    root = dataset_root or cfg.dataset_root()
    chosen_version = (version or cfg.DATASET_VERSION or "").strip()
    if not chosen_version:
        pointer = pointer_path(root)
        if not pointer.exists():
            raise FileNotFoundError(
                f"Dataset pointer not found at {pointer}. Publish a dataset first with phase2_publish_dataset.py."
            )
        pointer_payload = _read_json(pointer)
        chosen_version = str(pointer_payload.get("dataset_version", "")).strip()
        if not chosen_version:
            raise ValueError(f"Invalid pointer payload in {pointer}: missing dataset_version")

    metadata_path = root / "snapshots" / chosen_version / "metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Metadata not found for dataset version {chosen_version}: {metadata_path}")
    metadata = _read_json(metadata_path)

    prepared_uri = str(metadata.get("prepared_uri", "")).strip()
    if not prepared_uri:
        raise ValueError(f"Invalid metadata in {metadata_path}: missing prepared_uri")

    return DatasetRelease(
        version=chosen_version,
        created_at_utc=str(metadata.get("created_at_utc", "")),
        row_count=int(metadata.get("row_count", 0) or 0),
        schema_hash_sha256=str(metadata.get("schema_hash_sha256", "")),
        prepared_uri=prepared_uri,
        metadata_path=metadata_path,
    )


def _resolve_artifact_path(prepared_uri: str, dataset_root: Path) -> Path:
    if prepared_uri.startswith("file://"):
        return Path(prepared_uri[len("file://") :]).expanduser()
    candidate = Path(prepared_uri)
    if candidate.is_absolute():
        return candidate
    return (dataset_root / prepared_uri).resolve()


def _fallback_snapshot_artifact_path(release: DatasetRelease, dataset_root: Path, artifact: Path) -> Path:
    """Recover from machine-specific absolute paths embedded in snapshot metadata.

    Seed snapshots created on one machine may carry an absolute file path in
    metadata. When that snapshot is moved to a different environment, prefer an
    artifact with the same basename inside the current snapshot directory.
    """
    snapshot_candidate = (dataset_root / "snapshots" / release.version / artifact.name).resolve()
    return snapshot_candidate if snapshot_candidate.exists() else artifact


def validate_prepared_dataset(df: pd.DataFrame, expected_schema_hash: str | None = None) -> pd.DataFrame:
    missing = sorted(REQUIRED_PREPARED_COLUMNS - set(df.columns))
    if missing:
        raise ValueError(f"Prepared dataset missing required columns: {missing}")
    if df.empty:
        raise ValueError("Prepared dataset is empty")
    if expected_schema_hash:
        observed_hash = _schema_hash(df)
        if observed_hash != expected_schema_hash:
            raise ValueError(
                "Prepared dataset schema hash mismatch: "
                f"expected={expected_schema_hash}, observed={observed_hash}"
            )
    return df


def load_prepared_dataset(version: str | None = None, dataset_root: Path | None = None) -> tuple[pd.DataFrame, DatasetRelease]:
    root = dataset_root or cfg.dataset_root()
    release = resolve_release(version=version, dataset_root=root)
    if _is_remote_uri(release.prepared_uri):
        parsed = urlparse(release.prepared_uri)
        suffix = Path(parsed.path).suffix.lower()
        try:
            if suffix == ".parquet":
                df = pd.read_parquet(release.prepared_uri)
            elif suffix == ".csv":
                df = pd.read_csv(release.prepared_uri)
            else:
                raise ValueError(f"Unsupported artifact extension for remote uri: {release.prepared_uri}")
        except Exception as exc:
            raise RuntimeError(
                "Failed to read remote prepared_uri. Ensure storage backend dependencies are installed "
                "(for example: s3fs for S3, gcsfs for GCS, adlfs for Azure Blob). "
                f"prepared_uri={release.prepared_uri}"
            ) from exc
    else:
        artifact = _resolve_artifact_path(release.prepared_uri, root)
        if not artifact.exists():
            artifact = _fallback_snapshot_artifact_path(release, root, artifact)
        if not artifact.exists():
            raise FileNotFoundError(f"Prepared dataset artifact not found: {artifact}")

        if artifact.suffix.lower() == ".parquet":
            df = pd.read_parquet(artifact)
        elif artifact.suffix.lower() == ".csv":
            df = pd.read_csv(artifact)
        else:
            raise ValueError(f"Unsupported artifact extension for {artifact}")

    validate_prepared_dataset(df, expected_schema_hash=release.schema_hash_sha256)
    if emit_dataset_load_trace is not None:
        emit_dataset_load_trace(
            dataset_version=release.version,
            row_count=len(df),
            prepared_uri=release.prepared_uri,
        )
    return df, release


def release_summary(release: DatasetRelease, dataset_root: Path | None = None) -> dict[str, Any]:
    root = dataset_root or cfg.dataset_root()
    if _is_remote_uri(release.prepared_uri):
        artifact_path = release.prepared_uri
        artifact_sha = _sha256_uri(release.prepared_uri)
    else:
        artifact = _resolve_artifact_path(release.prepared_uri, root)
        if not artifact.exists():
            artifact = _fallback_snapshot_artifact_path(release, root, artifact)
        artifact_path = str(artifact)
        artifact_sha = _sha256_file(artifact)
    return {
        "dataset_version": release.version,
        "created_at_utc": release.created_at_utc,
        "row_count": release.row_count,
        "schema_hash_sha256": release.schema_hash_sha256,
        "prepared_uri": release.prepared_uri,
        "artifact_path": artifact_path,
        "artifact_sha256": artifact_sha,
        "metadata_path": str(release.metadata_path),
    }
