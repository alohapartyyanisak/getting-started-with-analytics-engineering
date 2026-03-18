from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sys

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import phase2_runtime_config as cfg
from phase2_managed_loader import load_prepared_dataset, pointer_path
from prod.rollback_snapshot import rollback_snapshot


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _available_versions(dataset_root: Path) -> list[str]:
    snapshots = dataset_root / "snapshots"
    if not snapshots.exists():
        return []
    return sorted([p.name for p in snapshots.iterdir() if p.is_dir()])


def run_dataset_pointer_rollback_drill() -> dict[str, Any]:
    dataset_root = cfg.dataset_root()
    versions = _available_versions(dataset_root)
    if len(versions) < 2:
        raise ValueError("Need at least 2 snapshot versions for rollback drill")

    pointer = pointer_path(dataset_root)
    before_pointer = _read_json(pointer)
    current_version = str(before_pointer.get("dataset_version", "") or "").strip()
    if current_version not in versions:
        raise ValueError(f"Current version {current_version} not found in snapshots")

    current_idx = versions.index(current_version)
    if current_idx == 0:
        raise ValueError("No previous snapshot available for rollback drill")
    target_version = versions[current_idx - 1]

    before_df, before_release = load_prepared_dataset(version=current_version, dataset_root=dataset_root)
    rollback_event: dict[str, Any] | None = None
    restore_event: dict[str, Any] | None = None
    target_df = None
    restored_df = None
    try:
        rollback_event = rollback_snapshot(
            target_version=target_version,
            steps_back=1,
            reason="rollback_drill_dataset",
        )
        target_df, target_release = load_prepared_dataset(version=target_version, dataset_root=dataset_root)
        restore_event = rollback_snapshot(
            target_version=current_version,
            steps_back=1,
            reason="rollback_drill_restore",
        )
        restored_df, restored_release = load_prepared_dataset(version=current_version, dataset_root=dataset_root)
    finally:
        after_pointer = _read_json(pointer)
        if str(after_pointer.get("dataset_version", "") or "").strip() != current_version:
            rollback_snapshot(
                target_version=current_version,
                steps_back=1,
                reason="rollback_drill_forced_restore",
            )
            after_pointer = _read_json(pointer)

    return {
        "status": "pass",
        "dataset_root": str(dataset_root.resolve()),
        "current_version": current_version,
        "target_version": target_version,
        "checks": {
            "before_load_ok": bool(len(before_df) > 0 and before_release.version == current_version),
            "target_load_ok": bool(target_df is not None and len(target_df) > 0),
            "restore_load_ok": bool(restored_df is not None and len(restored_df) > 0),
            "pointer_restored": str(_read_json(pointer).get("dataset_version", "") or "").strip() == current_version,
        },
        "rollback_event": rollback_event,
        "restore_event": restore_event,
        "rows": {
            "before": int(len(before_df)),
            "target": int(len(target_df)) if target_df is not None else None,
            "restored": int(len(restored_df)) if restored_df is not None else None,
        },
    }


if __name__ == "__main__":
    payload = run_dataset_pointer_rollback_drill()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
