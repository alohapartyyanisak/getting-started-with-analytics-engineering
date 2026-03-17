from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None


_ENV_LOADED = False


def _load_env_once() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    if load_dotenv is not None:
        load_dotenv()
    _ENV_LOADED = True


def _as_bool(value: str, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


_load_env_once()

REPO_ROOT = Path(__file__).resolve().parents[3]
GO_LIVE_DIR = REPO_ROOT / "recommendation_app" / "go-live"
OFFLINE_V2_CODE_DIR = REPO_ROOT / "recommendation_app" / "offline-v2" / "code"

# Local object-storage emulation for Phase 2.
DATASET_STORAGE_ROOT = Path(
    os.getenv("GO_LIVE_DATASET_STORAGE_ROOT", str(GO_LIVE_DIR / "data" / "storage"))
).expanduser()

# Pointer file name under DATASET_STORAGE_ROOT.
DATASET_POINTER_FILE = os.getenv("GO_LIVE_DATASET_POINTER_FILE", "latest.json").strip() or "latest.json"

# Optional forced version override for runtime.
DATASET_VERSION = os.getenv("GO_LIVE_DATASET_VERSION", "").strip()

# Optional explicit URI/path; when empty, DATASET_STORAGE_ROOT is used.
DATASET_URI = os.getenv("GO_LIVE_DATASET_URI", "").strip()

# Controls for publish script.
PUBLISH_INCLUDE_CSV = _as_bool(os.getenv("GO_LIVE_PUBLISH_INCLUDE_CSV", "true"), default=True)
PUBLISH_INCLUDE_PARQUET = _as_bool(os.getenv("GO_LIVE_PUBLISH_INCLUDE_PARQUET", "true"), default=True)

# Optional cloud artifact root for primary dataset artifacts (examples:
# s3://bucket/path, gs://bucket/path, abfs://container/path).
# When set, publish jobs can upload prepared artifacts and use cloud URIs in metadata.
PRIMARY_ARTIFACT_ROOT_URI = os.getenv("GO_LIVE_PRIMARY_ARTIFACT_ROOT_URI", "").strip()


def dataset_root() -> Path:
    if DATASET_URI:
        text = DATASET_URI
        if text.startswith("file://"):
            text = text[len("file://") :]
        return Path(text).expanduser()
    return DATASET_STORAGE_ROOT
