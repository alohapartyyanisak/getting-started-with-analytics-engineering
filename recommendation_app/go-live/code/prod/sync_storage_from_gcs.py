from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import phase2_runtime_config as cfg


def _as_bool(value: str, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def _normalize_gcs_prefix(uri: str) -> str:
    text = str(uri or "").strip()
    if not text.startswith("gs://"):
        raise ValueError(f"GCS_DATASET_URI must start with gs://, got: {text}")
    prefix = text[len("gs://") :].strip("/")
    if not prefix:
        raise ValueError("GCS_DATASET_URI must include a bucket name")
    return prefix


def sync_storage_from_gcs(gcs_uri: str, dataset_root: Path) -> dict[str, object]:
    try:
        import gcsfs  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("gcsfs is required to sync dataset storage from GCS") from exc

    fs = gcsfs.GCSFileSystem(token="cloud")
    prefix = _normalize_gcs_prefix(gcs_uri)
    files = sorted(fs.find(prefix))

    if not files:
        return {
            "status": "empty",
            "gcs_uri": gcs_uri,
            "dataset_root": str(dataset_root.resolve()),
            "files_copied": 0,
        }

    copied = 0
    bytes_copied = 0
    for remote_path in files:
        rel = remote_path[len(prefix) :].lstrip("/")
        if not rel:
            continue
        local_path = dataset_root / rel
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with fs.open(remote_path, "rb") as src:
            with local_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        copied += 1
        bytes_copied += int(local_path.stat().st_size)

    return {
        "status": "ok",
        "gcs_uri": gcs_uri,
        "dataset_root": str(dataset_root.resolve()),
        "files_copied": copied,
        "bytes_copied": bytes_copied,
    }


def main() -> int:
    gcs_uri = str(os.getenv("GCS_DATASET_URI", "") or "").strip()
    sync_enabled = _as_bool(os.getenv("GO_LIVE_SYNC_FROM_GCS", "true"), True)
    dataset_root = cfg.dataset_root()
    dataset_root.mkdir(parents=True, exist_ok=True)

    if not sync_enabled:
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": "GO_LIVE_SYNC_FROM_GCS disabled",
                    "dataset_root": str(dataset_root.resolve()),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not gcs_uri:
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": "GCS_DATASET_URI not set",
                    "dataset_root": str(dataset_root.resolve()),
                },
                ensure_ascii=False,
            )
        )
        return 0

    result = sync_storage_from_gcs(gcs_uri=gcs_uri, dataset_root=dataset_root)
    pointer = dataset_root / cfg.DATASET_POINTER_FILE
    if result.get("status") == "ok" and not pointer.exists():
        print(json.dumps(result, ensure_ascii=False))
        raise FileNotFoundError(f"Expected pointer file after GCS sync: {pointer}")

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
