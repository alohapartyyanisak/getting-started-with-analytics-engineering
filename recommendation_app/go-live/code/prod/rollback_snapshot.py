from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import phase2_runtime_config as cfg
from phase2_managed_loader import pointer_path, resolve_release


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _available_versions(dataset_root: Path) -> list[str]:
    snapshots = dataset_root / "snapshots"
    if not snapshots.exists():
        return []
    return sorted([p.name for p in snapshots.iterdir() if p.is_dir()])


def rollback_snapshot(target_version: str | None, steps_back: int, reason: str) -> dict[str, Any]:
    dataset_root = cfg.dataset_root()
    pointer = pointer_path(dataset_root)
    current_pointer = _read_json(pointer)
    current_version = str(current_pointer.get("dataset_version", "") or "").strip()
    if not current_version:
        raise ValueError("latest.json is missing dataset_version")

    versions = _available_versions(dataset_root)
    if not versions:
        raise ValueError("No snapshots available for rollback")

    if target_version:
        chosen = str(target_version).strip()
    else:
        if current_version not in versions:
            raise ValueError(f"Current version {current_version} not found in snapshots")
        current_idx = versions.index(current_version)
        target_idx = current_idx - max(1, int(steps_back))
        if target_idx < 0:
            raise ValueError("Rollback steps exceed available history")
        chosen = versions[target_idx]

    release = resolve_release(version=chosen, dataset_root=dataset_root)
    payload = {
        "dataset_version": release.version,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "metadata_uri": cfg.storage_ref(release.metadata_path, dataset_root),
    }
    _write_json(pointer, payload)

    event = {
        "status": "ok",
        "reason": reason,
        "rolled_back_from": current_version,
        "rolled_back_to": release.version,
        "pointer_path": str(pointer.resolve()),
        "available_versions": versions,
    }
    run_id = datetime.now(timezone.utc).strftime("prod_rollback_%Y%m%d_%H%M%S_utc")
    event_path = dataset_root / "prod_rollbacks" / f"{run_id}.json"
    _write_json(event_path, event)
    event["rollback_log_path"] = str(event_path.resolve())
    return event


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rollback prod dataset pointer to a prior snapshot")
    parser.add_argument("--target-version", default="")
    parser.add_argument("--steps-back", type=int, default=1)
    parser.add_argument("--reason", default="manual_rollback")
    args = parser.parse_args()

    payload = rollback_snapshot(
        target_version=str(args.target_version).strip() or None,
        steps_back=int(args.steps_back),
        reason=str(args.reason).strip() or "manual_rollback",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
