from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import sys

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import phase2_runtime_config as cfg
from phase2_managed_loader import load_prepared_dataset, pointer_path
from scheduler_orchestrator import orchestrate_scheduler  # type: ignore


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_prod_smoke(
    timeout_seconds: int = 300,
    problem_max_rows: int = 50,
    problem_max_resolve_keys: int = 20,
) -> dict[str, Any]:
    dataset_root = cfg.dataset_root()
    ptr_path = pointer_path(dataset_root)
    before_pointer = _read_json(ptr_path)
    before_version = str(before_pointer.get("dataset_version", "") or "").strip()

    frame, release = load_prepared_dataset(version=before_version, dataset_root=dataset_root)

    run = orchestrate_scheduler(
        mode="problem_queue_bounded",
        workers=16,
        force=False,
        behavioral_status="unknown",
        manual_status="unknown",
        auto_promote=False,
        publish_timeout_seconds=int(timeout_seconds),
        problem_max_rows=int(problem_max_rows),
        problem_max_resolve_keys=int(problem_max_resolve_keys),
    )

    after_pointer = _read_json(ptr_path)
    after_version = str(after_pointer.get("dataset_version", "") or "").strip()

    candidate = str(run.get("candidate_dataset_version", "") or "").strip()
    report_path = str((run.get("publish_result", {}) or {}).get("report_path", "") or "").strip()
    validation_path = str(run.get("validation_summary_path", "") or "").strip()
    run_log_path = str(run.get("run_log_path", "") or "").strip()

    checks = {
        "approved_snapshot_loads": bool(len(frame) > 0 and release.version == before_version),
        "scheduler_entrypoint_executed": str(run.get("status", "")) in {"ok", "noop", "no_changes", "dry_run"},
        "candidate_generated": bool(candidate),
        "report_generated": bool(report_path and Path(report_path).exists()),
        "validation_summary_generated": bool(validation_path and Path(validation_path).exists()),
        "run_log_generated": bool(run_log_path and Path(run_log_path).exists()),
        "pointer_unchanged": before_version == after_version,
        "no_user_facing_promotion": bool(run.get("promoted") is False),
    }

    status = "pass" if all(checks.values()) else "fail"
    return {
        "status": status,
        "base_dataset_version": before_version,
        "pointer_after": after_version,
        "checks": checks,
        "orchestrator_result": run,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prod-like dry run smoke check without promotion")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--problem-max-rows", type=int, default=50)
    parser.add_argument("--problem-max-resolve-keys", type=int, default=20)
    args = parser.parse_args()

    payload = run_prod_smoke(
        timeout_seconds=int(args.timeout_seconds),
        problem_max_rows=int(args.problem_max_rows),
        problem_max_resolve_keys=int(args.problem_max_resolve_keys),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if payload.get("status") == "pass" else 1)
