from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import phase2_runtime_config as cfg
from phase2_managed_loader import pointer_path, resolve_release
from phase2_weekly_validation_summary import build_validation_summary


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _log_release_event(dataset_root: Path, payload: dict[str, Any]) -> str:
    run_id = datetime.now(timezone.utc).strftime("prod_release_%Y%m%d_%H%M%S_utc")
    log_path = dataset_root / "prod_releases" / f"{run_id}.json"
    _write_json(log_path, payload)
    return str(log_path.resolve())


def promote_snapshot(candidate_version: str, base_version: str | None, behavioral_status: str, manual_status: str) -> dict[str, Any]:
    dataset_root = cfg.dataset_root()
    pointer = pointer_path(dataset_root)
    current_pointer = _read_json(pointer)
    resolved_base = str(base_version or current_pointer.get("dataset_version", "")).strip()
    if not resolved_base:
        raise ValueError("base_version is required when latest.json has no dataset_version")

    validation_summary = build_validation_summary(
        candidate_version=candidate_version,
        base_version=resolved_base,
        behavioral_status=behavioral_status,
        manual_status=manual_status,
    )
    if validation_summary.get("publish_recommended") is not True:
        blocked_payload = {
            "status": "blocked",
            "reason": "VALIDATION_NOT_APPROVED",
            "current_dataset_version": str(current_pointer.get("dataset_version", "") or ""),
            "candidate_dataset_version": candidate_version,
            "validation_summary": validation_summary,
        }
        blocked_payload["release_log_path"] = _log_release_event(dataset_root, blocked_payload)
        return blocked_payload

    release = resolve_release(version=candidate_version, dataset_root=dataset_root)
    metadata_uri = f"file://{release.metadata_path.resolve()}"
    payload = {
        "dataset_version": release.version,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "metadata_uri": metadata_uri,
    }
    _write_json(pointer, payload)
    success_payload = {
        "status": "ok",
        "current_dataset_version": str(current_pointer.get("dataset_version", "") or ""),
        "promoted_dataset_version": release.version,
        "pointer_path": str(pointer.resolve()),
        "validation_summary": validation_summary,
    }
    success_payload["release_log_path"] = _log_release_event(dataset_root, success_payload)
    return success_payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Promote an already validated snapshot in prod")
    parser.add_argument("--candidate-version", required=True)
    parser.add_argument("--base-version", default="")
    parser.add_argument("--behavioral-status", choices=("unknown", "passed", "failed"), default="passed")
    parser.add_argument("--manual-status", choices=("unknown", "approved", "rejected"), default="approved")
    args = parser.parse_args()

    payload = promote_snapshot(
        candidate_version=str(args.candidate_version).strip(),
        base_version=str(args.base_version).strip() or None,
        behavioral_status=str(args.behavioral_status).strip(),
        manual_status=str(args.manual_status).strip(),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
