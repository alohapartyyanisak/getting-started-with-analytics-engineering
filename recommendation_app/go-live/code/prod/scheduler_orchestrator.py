from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pandas as pd

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import phase2_runtime_config as cfg
from phase2_managed_loader import load_prepared_dataset, pointer_path
from phase2_weekly_validation_summary import build_validation_rows, build_validation_summary

import phase2_incremental_publish
import phase2_publish_dataset
import phase2_revalidate_problem_queue
import phase2_unresolved_problem_queue


PUBLISH_MODES = ("incremental", "full", "problem_queue", "problem_queue_bounded")
DEFAULT_BOUNDED_MAX_ROWS = 1200
DEFAULT_BOUNDED_MAX_RESOLVE_KEYS = 400
DEFAULT_PUBLISH_TIMEOUT_SECONDS = 1800
PROBLEM_QUEUE_REQUIRED_REPORT_COLUMNS = {
    "status",
    "canonical_key",
    "artist",
    "track",
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def _current_pointer_payload(dataset_root: Path) -> dict[str, Any]:
    path = pointer_path(dataset_root)
    if not path.exists():
        raise FileNotFoundError(f"Dataset pointer not found: {path}")
    return _read_json(path)


def _restore_pointer(dataset_root: Path, payload: dict[str, Any]) -> None:
    _write_json(pointer_path(dataset_root), payload)


def _verify_candidate_loads(dataset_root: Path, version: str) -> dict[str, Any]:
    frame, release = load_prepared_dataset(version=version, dataset_root=dataset_root)
    return {
        "dataset_version": release.version,
        "rows_prepared": int(len(frame)),
        "artists": int(frame["artist"].nunique()),
        "tracks": int(frame["display_name"].nunique()),
    }


def _path_from_text(value: object) -> Path:
    text = str(value or "").strip()
    if not text:
        return Path("")
    if text.startswith("file://"):
        parsed = urlparse(text)
        return Path(unquote(parsed.path)).expanduser()
    return Path(text).expanduser()


def _resolve_problem_queue_report_path(dataset_root: Path, release_version: str) -> tuple[Path, str]:
    direct_path = dataset_root / "snapshots" / release_version / "youtube_link_revalidation_report.csv"
    if direct_path.exists():
        return direct_path.resolve(), "current_snapshot"

    metadata_path = dataset_root / "snapshots" / release_version / "metadata.json"
    if metadata_path.exists():
        metadata = _read_json(metadata_path)
        pq_meta = metadata.get("problem_queue_revalidation") or {}
        input_report = _path_from_text(pq_meta.get("input_report", ""))
        if input_report.exists():
            return input_report.resolve(), "metadata_input_report"

        source_meta = metadata.get("source") or {}
        parent_version = str(source_meta.get("parent_dataset_version", "") or "").strip()
        if parent_version:
            parent_path = dataset_root / "snapshots" / parent_version / "youtube_link_revalidation_report.csv"
            if parent_path.exists():
                return parent_path.resolve(), "parent_snapshot"

    raise FileNotFoundError(f"Revalidation report not found for release {release_version}")


def _read_csv_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
    return [str(col or "").strip() for col in header if str(col or "").strip()]


def _preflight_check(mode: str, dataset_root: Path, base_version: str) -> dict[str, Any]:
    if mode not in {"problem_queue", "problem_queue_bounded"}:
        return {
            "required": False,
            "mode": mode,
            "base_dataset_version": base_version,
        }

    report_path, report_source = _resolve_problem_queue_report_path(dataset_root, base_version)
    header = _read_csv_header(report_path)
    header_set = set(header)
    missing = sorted(PROBLEM_QUEUE_REQUIRED_REPORT_COLUMNS - header_set)
    if missing:
        raise ValueError(
            "Revalidation report is missing required columns: "
            + ", ".join(missing)
            + f" (path={report_path})"
        )

    return {
        "required": True,
        "mode": mode,
        "base_dataset_version": base_version,
        "report_path": str(report_path),
        "report_source": report_source,
        "required_columns": sorted(PROBLEM_QUEUE_REQUIRED_REPORT_COLUMNS),
        "missing_columns": [],
    }


def _post_run_smoke_check(dataset_root: Path, expected_pointer_version: str) -> dict[str, Any]:
    pointer = _current_pointer_payload(dataset_root)
    pointer_version = str(pointer.get("dataset_version", "") or "").strip()
    if not pointer_version:
        raise ValueError("Post-run smoke failed: pointer has no dataset_version")

    frame, release = load_prepared_dataset(version=pointer_version, dataset_root=dataset_root)
    checks = {
        "pointer_present": bool(pointer_version),
        "pointer_matches_expected": bool(pointer_version == str(expected_pointer_version or "").strip()),
        "active_snapshot_loads": bool(len(frame) > 0 and release.version == pointer_version),
    }
    status = "pass" if all(checks.values()) else "fail"
    if status != "pass":
        raise RuntimeError(
            "Post-run smoke failed: "
            + json.dumps(
                {
                    "expected_pointer_version": expected_pointer_version,
                    "pointer_version": pointer_version,
                    "checks": checks,
                },
                ensure_ascii=False,
            )
        )

    return {
        "status": status,
        "expected_pointer_version": str(expected_pointer_version or "").strip(),
        "pointer_version": pointer_version,
        "rows_prepared": int(len(frame)),
        "checks": checks,
    }


def _start_stage(name: str) -> dict[str, Any]:
    return {
        "stage": name,
        "started_at_utc": _iso_now(),
        "_started_perf": time.perf_counter(),
    }


def _end_stage(entry: dict[str, Any], status: str, details: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
    elapsed = int((time.perf_counter() - float(entry.get("_started_perf", time.perf_counter()))) * 1000)
    out = {
        "stage": entry.get("stage"),
        "started_at_utc": entry.get("started_at_utc"),
        "ended_at_utc": _iso_now(),
        "elapsed_ms": elapsed,
        "status": status,
    }
    if details:
        out["details"] = details
    if error:
        out["error"] = error
    return out


def _run_publish_step(
    mode: str,
    force: bool,
    workers: int,
    problem_max_rows: int,
    problem_max_resolve_keys: int,
) -> dict[str, Any]:
    if mode == "incremental":
        return phase2_incremental_publish.incremental_publish(force=force)
    if mode == "full":
        return phase2_publish_dataset.publish_dataset()
    if mode in {"problem_queue", "problem_queue_bounded"}:
        max_rows = int(problem_max_rows)
        max_keys = int(problem_max_resolve_keys)
        if mode == "problem_queue_bounded":
            if max_rows <= 0:
                max_rows = DEFAULT_BOUNDED_MAX_ROWS
            if max_keys <= 0:
                max_keys = DEFAULT_BOUNDED_MAX_RESOLVE_KEYS
        return phase2_revalidate_problem_queue.revalidate_problem_queue(
            dry_run=False,
            workers=workers,
            max_problem_rows=max(0, max_rows),
            max_resolve_keys=max(0, max_keys),
        )
    raise ValueError(f"Unsupported publish mode: {mode}")


def _publish_worker(
    mode: str,
    force: bool,
    workers: int,
    problem_max_rows: int,
    problem_max_resolve_keys: int,
    queue: Any,
) -> None:
    try:
        result = _run_publish_step(
            mode=mode,
            force=force,
            workers=workers,
            problem_max_rows=problem_max_rows,
            problem_max_resolve_keys=problem_max_resolve_keys,
        )
        queue.put({"ok": True, "result": result})
    except Exception as exc:
        queue.put(
            {
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )


def _run_publish_step_with_timeout(
    mode: str,
    force: bool,
    workers: int,
    problem_max_rows: int,
    problem_max_resolve_keys: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    timeout_s = max(1, int(timeout_seconds))
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(
        target=_publish_worker,
        args=(mode, force, workers, problem_max_rows, problem_max_resolve_keys, queue),
    )
    process.start()
    process.join(timeout=timeout_s)

    if process.is_alive():
        process.terminate()
        process.join(5)
        raise TimeoutError(f"publish stage timeout after {timeout_s}s in mode={mode}")

    if queue.empty():
        raise RuntimeError(f"publish stage exited without payload (exitcode={process.exitcode})")

    payload = queue.get()
    if not bool(payload.get("ok")):
        error = str(payload.get("error", "publish stage failed"))
        tb = str(payload.get("traceback", ""))
        raise RuntimeError(f"{error}\n{tb}".strip())

    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("publish stage returned invalid payload")
    return result


def orchestrate_scheduler(
    mode: str,
    workers: int = 16,
    force: bool = False,
    behavioral_status: str = "unknown",
    manual_status: str = "unknown",
    auto_promote: bool = False,
    publish_timeout_seconds: int = DEFAULT_PUBLISH_TIMEOUT_SECONDS,
    problem_max_rows: int = 0,
    problem_max_resolve_keys: int = 0,
    run_post_smoke: bool = True,
) -> dict[str, Any]:
    if mode not in PUBLISH_MODES:
        raise ValueError(f"mode must be one of: {PUBLISH_MODES}")

    dataset_root = cfg.dataset_root()
    run_id = datetime.now(timezone.utc).strftime("prod_sched_%Y%m%d_%H%M%S_utc")
    run_log_path = dataset_root / "prod_runs" / f"{run_id}.json"
    stages: list[dict[str, Any]] = []

    base_pointer: dict[str, Any] = {}
    base_version = ""
    pointer_restored = False
    promoted = False
    publish_result: dict[str, Any] = {}
    candidate_version = ""

    try:
        stage = _start_stage("load_base_pointer")
        base_pointer = _current_pointer_payload(dataset_root)
        base_version = str(base_pointer.get("dataset_version", "")).strip()
        if not base_version:
            raise ValueError("Current dataset pointer is missing dataset_version")
        stages.append(_end_stage(stage, "ok", details={"base_dataset_version": base_version}))

        stage = _start_stage("preflight")
        try:
            preflight_result = _preflight_check(mode=mode, dataset_root=dataset_root, base_version=base_version)
            stages.append(_end_stage(stage, "ok", details=preflight_result))
        except Exception as exc:
            stages.append(_end_stage(stage, "error", error=str(exc)))
            raise

        stage = _start_stage("publish_stage")
        publish_result = _run_publish_step_with_timeout(
            mode=mode,
            force=force,
            workers=workers,
            problem_max_rows=problem_max_rows,
            problem_max_resolve_keys=problem_max_resolve_keys,
            timeout_seconds=publish_timeout_seconds,
        )
        stages.append(
            _end_stage(
                stage,
                "ok",
                details={
                    "publish_status": str(publish_result.get("status", "") or ""),
                    "dataset_version": str(publish_result.get("dataset_version", "") or ""),
                },
            )
        )

        status = str(publish_result.get("status", "") or "")
        if status in {"noop", "no_changes", "dry_run"}:
            pointer_after = _current_pointer_payload(dataset_root)
            post_smoke_result: dict[str, Any] = {"status": "skipped", "reason": "run_post_smoke_disabled"}
            if run_post_smoke:
                stage = _start_stage("post_run_smoke")
                try:
                    post_smoke_result = _post_run_smoke_check(
                        dataset_root=dataset_root,
                        expected_pointer_version=str(pointer_after.get("dataset_version", "") or ""),
                    )
                    stages.append(_end_stage(stage, "ok", details=post_smoke_result))
                except Exception as exc:
                    stages.append(_end_stage(stage, "error", error=str(exc)))
                    raise
            payload = {
                "status": status,
                "run_id": run_id,
                "mode": mode,
                "base_dataset_version": base_version,
                "publish_result": publish_result,
                "pointer_after": pointer_after,
                "post_run_smoke": post_smoke_result,
                "promoted": False,
                "stages": stages,
                "run_log_path": str(run_log_path.resolve()),
            }
            _write_json(run_log_path, payload)
            return payload

        candidate_version = str(publish_result.get("dataset_version", "")).strip()
        if not candidate_version:
            raise ValueError(f"Publish step did not return dataset_version: {publish_result}")

        stage = _start_stage("candidate_health")
        candidate_health = _verify_candidate_loads(dataset_root, candidate_version)
        stages.append(_end_stage(stage, "ok", details=candidate_health))

        stage = _start_stage("validation_summary")
        validation_summary = build_validation_summary(
            candidate_version=candidate_version,
            base_version=base_version,
            behavioral_status=behavioral_status,
            manual_status=manual_status,
        )
        validation_summary_path = dataset_root / "snapshots" / candidate_version / "weekly_validation_summary.json"
        _write_json(validation_summary_path, validation_summary)
        stages.append(
            _end_stage(
                stage,
                "ok",
                details={
                    "publish_recommended": bool(validation_summary.get("publish_recommended") is True),
                    "quality_validation_passed": bool(validation_summary.get("quality_validation_passed") is True),
                    "contract_validation_passed": bool(validation_summary.get("contract_validation_passed") is True),
                    "validation_summary_path": str(validation_summary_path.resolve()),
                },
            )
        )

        stage = _start_stage("validation_rows")
        validation_rows = build_validation_rows(
            candidate_version=candidate_version,
            base_version=base_version,
            execution_mode=mode,
            validation_run_id=run_id,
        )
        validation_rows_path = dataset_root / "snapshots" / candidate_version / "validation_rows.parquet"
        _write_parquet(validation_rows_path, validation_rows)
        stages.append(
            _end_stage(
                stage,
                "ok",
                details={
                    "validation_rows_path": str(validation_rows_path.resolve()),
                    "row_count": int(len(validation_rows)),
                },
            )
        )

        stage = _start_stage("unresolved_queue")
        unresolved_queue_result = phase2_unresolved_problem_queue.update_unresolved_problem_queue(
            validation_rows=validation_rows,
            dataset_root=dataset_root,
            run_id=run_id,
            candidate_dataset_version=candidate_version,
            base_dataset_version=base_version,
        )
        stages.append(
            _end_stage(
                stage,
                "ok",
                details={
                    "current_queue_path": unresolved_queue_result["current_queue_path"],
                    "current_summary_path": unresolved_queue_result["current_summary_path"],
                    "history_queue_path": unresolved_queue_result["history_queue_path"],
                    "history_summary_path": unresolved_queue_result["history_summary_path"],
                    "row_count": int(unresolved_queue_result["row_count"]),
                },
            )
        )

        promoted = bool(auto_promote and validation_summary.get("publish_recommended") is True)
        if not promoted:
            stage = _start_stage("pointer_restore")
            _restore_pointer(dataset_root, base_pointer)
            pointer_restored = True
            stages.append(_end_stage(stage, "ok", details={"restored_to": base_version}))

        pointer_after = _current_pointer_payload(dataset_root)
        post_smoke_result: dict[str, Any] = {"status": "skipped", "reason": "run_post_smoke_disabled"}
        if run_post_smoke:
            stage = _start_stage("post_run_smoke")
            try:
                post_smoke_result = _post_run_smoke_check(
                    dataset_root=dataset_root,
                    expected_pointer_version=str(pointer_after.get("dataset_version", "") or ""),
                )
                stages.append(_end_stage(stage, "ok", details=post_smoke_result))
            except Exception as exc:
                stages.append(_end_stage(stage, "error", error=str(exc)))
                raise

        payload = {
            "status": "ok",
            "run_id": run_id,
            "mode": mode,
            "base_dataset_version": base_version,
            "candidate_dataset_version": candidate_version,
            "candidate_health": candidate_health,
            "validation_summary_path": str(validation_summary_path.resolve()),
            "validation_rows_path": str(validation_rows_path.resolve()),
            "unresolved_queue_current_path": unresolved_queue_result["current_queue_path"],
            "unresolved_queue_summary_path": unresolved_queue_result["current_summary_path"],
            "unresolved_queue_history_path": unresolved_queue_result["history_queue_path"],
            "unresolved_queue_history_summary_path": unresolved_queue_result["history_summary_path"],
            "unresolved_queue_summary": unresolved_queue_result["summary"],
            "validation_summary": validation_summary,
            "promoted": promoted,
            "pointer_restored": pointer_restored,
            "pointer_after": pointer_after,
            "post_run_smoke": post_smoke_result,
            "publish_result": publish_result,
            "stages": stages,
            "run_log_path": str(run_log_path.resolve()),
        }
        _write_json(run_log_path, payload)
        return payload

    except Exception as exc:
        err = str(exc)
        if base_pointer and not promoted:
            try:
                _restore_pointer(dataset_root, base_pointer)
                pointer_restored = True
            except Exception:
                pass

        payload = {
            "status": "error",
            "run_id": run_id,
            "mode": mode,
            "base_dataset_version": base_version,
            "candidate_dataset_version": candidate_version,
            "promoted": False,
            "pointer_restored": pointer_restored,
            "pointer_after": _current_pointer_payload(dataset_root) if pointer_path(dataset_root).exists() else {},
            "publish_result": publish_result,
            "stages": stages,
            "error": err,
            "error_type": exc.__class__.__name__,
            "run_log_path": str(run_log_path.resolve()),
        }
        _write_json(run_log_path, payload)
        return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prod scheduler orchestration for snapshot publish + validation")
    parser.add_argument("--mode", choices=PUBLISH_MODES, required=True)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--behavioral-status", choices=("unknown", "passed", "failed"), default="unknown")
    parser.add_argument("--manual-status", choices=("unknown", "approved", "rejected"), default="unknown")
    parser.add_argument("--auto-promote", action="store_true")
    parser.add_argument("--publish-timeout-seconds", type=int, default=DEFAULT_PUBLISH_TIMEOUT_SECONDS)
    parser.add_argument("--problem-max-rows", type=int, default=0)
    parser.add_argument("--problem-max-resolve-keys", type=int, default=0)
    parser.add_argument("--skip-post-smoke", action="store_true")
    args = parser.parse_args()

    payload = orchestrate_scheduler(
        mode=str(args.mode),
        workers=int(args.workers),
        force=bool(args.force),
        behavioral_status=str(args.behavioral_status),
        manual_status=str(args.manual_status),
        auto_promote=bool(args.auto_promote),
        publish_timeout_seconds=int(args.publish_timeout_seconds),
        problem_max_rows=int(args.problem_max_rows),
        problem_max_resolve_keys=int(args.problem_max_resolve_keys),
        run_post_smoke=not bool(args.skip_post_smoke),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
