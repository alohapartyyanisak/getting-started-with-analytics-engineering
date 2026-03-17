from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sys

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import phase2_runtime_config as cfg
from phase2_managed_loader import pointer_path


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_created(meta_path: Path) -> datetime | None:
    try:
        meta = _read_json(meta_path)
    except Exception:
        return None
    raw = str(meta.get("created_at_utc", "") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_prod_run_ts(file_name: str) -> datetime | None:
    match = re.match(r"^prod_sched_(\d{8})_(\d{6})_utc\.json$", str(file_name or ""))
    if not match:
        return None
    date_part = match.group(1)
    time_part = match.group(2)
    try:
        dt = datetime.strptime(f"{date_part}{time_part}", "%Y%m%d%H%M%S")
    except Exception:
        return None
    return dt.replace(tzinfo=timezone.utc)


def _collect_promoted_versions(releases_dir: Path) -> set[str]:
    promoted: set[str] = set()
    for path in sorted(releases_dir.glob("*.json")):
        try:
            payload = _read_json(path)
        except Exception:
            continue
        version = str(payload.get("promoted_dataset_version", "") or "").strip()
        if version:
            promoted.add(version)
    return promoted


def _collect_dry_run_candidates(runs_dir: Path) -> dict[str, datetime]:
    # Keep the latest seen run timestamp per candidate version.
    candidates: dict[str, datetime] = {}
    for path in sorted(runs_dir.glob("*.json")):
        try:
            payload = _read_json(path)
        except Exception:
            continue
        candidate = str(payload.get("candidate_dataset_version", "") or "").strip()
        promoted = bool(payload.get("promoted") is True)
        if not candidate or promoted:
            continue
        ts = _parse_prod_run_ts(path.name)
        if ts is None:
            ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        previous = candidates.get(candidate)
        if previous is None or ts > previous:
            candidates[candidate] = ts
    return candidates


def run_retention(
    retention_days: int = 60,
    dry_run_snapshot_retention_days: int = 14,
    run_log_retention_days: int = 30,
    apply: bool = False,
) -> dict[str, Any]:
    dataset_root = cfg.dataset_root()
    snapshots_dir = dataset_root / "snapshots"
    runs_dir = dataset_root / "prod_runs"
    releases_dir = dataset_root / "prod_releases"
    ptr = _read_json(pointer_path(dataset_root))
    active = str(ptr.get("dataset_version", "") or "").strip()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(retention_days)))
    dry_run_cutoff = now - timedelta(days=max(1, int(dry_run_snapshot_retention_days)))
    run_log_cutoff = now - timedelta(days=max(1, int(run_log_retention_days)))
    scanned = 0
    keep: list[str] = []
    delete: list[str] = []
    unknown_age: list[str] = []

    for path in sorted(snapshots_dir.glob("ds_*")):
        if not path.is_dir():
            continue
        version = path.name
        scanned += 1
        if version == active:
            keep.append(version)
            continue
        created = _parse_created(path / "metadata.json")
        if created is None:
            unknown_age.append(version)
            keep.append(version)
            continue
        if created >= cutoff:
            keep.append(version)
        else:
            delete.append(version)

    promoted_versions = _collect_promoted_versions(releases_dir)
    dry_run_candidates = _collect_dry_run_candidates(runs_dir)
    dry_run_candidates_existing = {
        version: ts for version, ts in dry_run_candidates.items() if (snapshots_dir / version).exists()
    }
    dry_run_snapshot_delete: list[str] = []
    dry_run_snapshot_keep: list[str] = []
    for version, candidate_ts in sorted(dry_run_candidates_existing.items()):
        if version == active:
            dry_run_snapshot_keep.append(version)
            continue
        if version in promoted_versions:
            dry_run_snapshot_keep.append(version)
            continue
        if candidate_ts < dry_run_cutoff:
            dry_run_snapshot_delete.append(version)
        else:
            dry_run_snapshot_keep.append(version)

    run_logs_delete: list[str] = []
    run_logs_keep: list[str] = []
    for path in sorted(runs_dir.glob("*.json")):
        ts = _parse_prod_run_ts(path.name)
        if ts is None:
            ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if ts < run_log_cutoff:
            run_logs_delete.append(path.name)
        else:
            run_logs_keep.append(path.name)

    deleted: list[str] = []
    dry_run_deleted: list[str] = []
    run_logs_deleted: list[str] = []
    if apply:
        for version in delete:
            target = snapshots_dir / version
            if target.exists() and target.is_dir():
                shutil.rmtree(target)
                deleted.append(version)
        for version in dry_run_snapshot_delete:
            target = snapshots_dir / version
            if target.exists() and target.is_dir():
                shutil.rmtree(target)
                dry_run_deleted.append(version)
        for file_name in run_logs_delete:
            target = runs_dir / file_name
            if target.exists() and target.is_file():
                target.unlink()
                run_logs_deleted.append(file_name)

    return {
        "status": "ok",
        "retention_days": int(retention_days),
        "dry_run_snapshot_retention_days": int(dry_run_snapshot_retention_days),
        "run_log_retention_days": int(run_log_retention_days),
        "apply": bool(apply),
        "cutoff_utc": cutoff.isoformat(),
        "dry_run_snapshot_cutoff_utc": dry_run_cutoff.isoformat(),
        "run_log_cutoff_utc": run_log_cutoff.isoformat(),
        "active_dataset_version": active,
        "scanned_snapshots": scanned,
        "kept_versions": keep,
        "delete_candidates": delete,
        "deleted_versions": deleted,
        "unknown_age_versions": unknown_age,
        "dry_run_candidate_versions": sorted(dry_run_candidates_existing.keys()),
        "promoted_versions": sorted(promoted_versions),
        "dry_run_snapshot_keep": dry_run_snapshot_keep,
        "dry_run_snapshot_delete_candidates": dry_run_snapshot_delete,
        "dry_run_snapshot_deleted": dry_run_deleted,
        "run_logs_keep": run_logs_keep,
        "run_logs_delete_candidates": run_logs_delete,
        "run_logs_deleted": run_logs_deleted,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Snapshot and prod run-log retention utility")
    parser.add_argument("--retention-days", type=int, default=60)
    parser.add_argument("--dry-run-snapshot-retention-days", type=int, default=14)
    parser.add_argument("--run-log-retention-days", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="Actually delete snapshots older than retention window")
    args = parser.parse_args()

    payload = run_retention(
        retention_days=int(args.retention_days),
        dry_run_snapshot_retention_days=int(args.dry_run_snapshot_retention_days),
        run_log_retention_days=int(args.run_log_retention_days),
        apply=bool(args.apply),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
