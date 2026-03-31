from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _code_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _copy_storage_tree() -> Path:
    repo_root = _repo_root()
    source = repo_root / "recommendation_app" / "go-live" / "data" / "storage"
    temp_root = Path(tempfile.mkdtemp(prefix="go-live-scheduler-smoke-"))
    target = temp_root / "storage"
    shutil.copytree(source, target)
    return target


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_scheduler_output_artifact_smoke(
    mode: str = "full",
    timeout_seconds: int = 300,
    problem_max_rows: int = 50,
    problem_max_resolve_keys: int = 20,
) -> dict[str, Any]:
    dataset_root = _copy_storage_tree()
    os.environ["GO_LIVE_DATASET_STORAGE_ROOT"] = str(dataset_root)

    code_root = _code_root()
    if str(code_root) not in sys.path:
        sys.path.insert(0, str(code_root))

    import phase2_runtime_config as cfg
    from phase2_managed_loader import pointer_path
    from scheduler_orchestrator import orchestrate_scheduler

    active_dataset_root = cfg.dataset_root()
    before_pointer = _read_json(pointer_path(active_dataset_root))
    before_version = str(before_pointer.get("dataset_version", "") or "").strip()

    run = orchestrate_scheduler(
        mode=str(mode or "full").strip(),
        workers=16,
        force=False,
        behavioral_status="unknown",
        manual_status="unknown",
        auto_promote=False,
        publish_timeout_seconds=int(timeout_seconds),
        problem_max_rows=int(problem_max_rows),
        problem_max_resolve_keys=int(problem_max_resolve_keys),
    )

    after_pointer = _read_json(pointer_path(active_dataset_root))
    after_version = str(after_pointer.get("dataset_version", "") or "").strip()

    validation_rows_path = Path(str(run.get("validation_rows_path", "") or ""))
    queue_current_path = Path(str(run.get("unresolved_queue_current_path", "") or ""))
    queue_summary_path = Path(str(run.get("unresolved_queue_summary_path", "") or ""))
    queue_history_path = Path(str(run.get("unresolved_queue_history_path", "") or ""))
    queue_history_summary_path = Path(str(run.get("unresolved_queue_history_summary_path", "") or ""))
    run_log_path = Path(str(run.get("run_log_path", "") or ""))

    queue_summary = _read_json(queue_summary_path) if queue_summary_path.is_file() else {}
    checks = {
        "scheduler_ok": str(run.get("status", "")) == "ok",
        "pointer_unchanged": before_version == after_version,
        "validation_rows_written": validation_rows_path.is_file(),
        "queue_current_written": queue_current_path.is_file(),
        "queue_summary_written": queue_summary_path.is_file(),
        "queue_history_written": queue_history_path.is_file(),
        "queue_history_summary_written": queue_history_summary_path.is_file(),
        "run_log_written": run_log_path.is_file(),
        "queue_summary_candidate_matches": str(queue_summary.get("candidate_dataset_version", "") or "").strip()
        == str(run.get("candidate_dataset_version", "") or "").strip(),
        "queue_summary_has_counts": isinstance(queue_summary.get("open_count"), int)
        and isinstance(queue_summary.get("resolved_count"), int),
    }
    status = "pass" if all(checks.values()) else "fail"
    return {
        "status": status,
        "dataset_root": str(active_dataset_root.resolve()),
        "checks": checks,
        "orchestrator_result": run,
        "queue_summary": queue_summary,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke-check scheduler output artifact paths on a temp storage copy")
    parser.add_argument("--mode", choices=("incremental", "full", "problem_queue", "problem_queue_bounded"), default="full")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--problem-max-rows", type=int, default=50)
    parser.add_argument("--problem-max-resolve-keys", type=int, default=20)
    args = parser.parse_args()

    payload = run_scheduler_output_artifact_smoke(
        mode=str(args.mode).strip(),
        timeout_seconds=int(args.timeout_seconds),
        problem_max_rows=int(args.problem_max_rows),
        problem_max_resolve_keys=int(args.problem_max_resolve_keys),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if payload.get("status") == "pass" else 1)
