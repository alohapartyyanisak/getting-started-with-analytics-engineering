from __future__ import annotations

import json
from pathlib import Path

import phase2_runtime_config as cfg
from phase2_managed_loader import load_prepared_dataset, pointer_path


def _available_versions(dataset_root: Path) -> list[str]:
    snapshots = dataset_root / "snapshots"
    if not snapshots.exists():
        return []
    return sorted([p.name for p in snapshots.iterdir() if p.is_dir()])


def run_rollback_smoke() -> int:
    root = cfg.dataset_root()
    versions = _available_versions(root)
    if len(versions) < 2:
        payload = {
            "status": "skip",
            "reason": "Need at least 2 snapshot versions for rollback smoke test.",
            "dataset_root": str(root.resolve()),
            "available_versions": versions,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    latest = versions[-1]
    previous = versions[-2]
    latest_df, latest_release = load_prepared_dataset(version=latest, dataset_root=root)
    prev_df, prev_release = load_prepared_dataset(version=previous, dataset_root=root)

    payload = {
        "status": "pass",
        "dataset_root": str(root.resolve()),
        "pointer_path": str(pointer_path(root).resolve()),
        "latest_version": latest_release.version,
        "previous_version": prev_release.version,
        "latest_rows": int(len(latest_df)),
        "previous_rows": int(len(prev_df)),
        "rollback_ready": True,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_rollback_smoke())

