from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return text


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = _clean_text(value).lower()
    return text in {"1", "true", "yes", "y", "on"}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def _queue_root(dataset_root: Path) -> Path:
    return dataset_root / "prod_recovery" / "unresolved_queue"


def _current_queue_path(dataset_root: Path) -> Path:
    return _queue_root(dataset_root) / "current.parquet"


def _current_summary_path(dataset_root: Path) -> Path:
    return _queue_root(dataset_root) / "current_summary.json"


def _history_dir(dataset_root: Path, timestamp: datetime) -> Path:
    return _queue_root(dataset_root) / "history" / timestamp.strftime("%Y") / timestamp.strftime("%m") / timestamp.strftime("%d")


def _empty_queue_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "queue_id",
            "yt_key",
            "artist",
            "track",
            "version_tag",
            "dataset_snapshot_id",
            "queue_status",
            "problem_type",
            "problem_reason",
            "current_watch_url",
            "current_video_id",
            "current_source_type",
            "first_seen_at_utc",
            "last_seen_at_utc",
            "last_attempted_at_utc",
            "attempt_count",
            "last_attempt_outcome",
            "last_error_code",
            "candidate_dataset_version",
            "base_dataset_version",
            "row_confidence_band",
            "golden_song_flag",
            "manual_review_required",
            "resolved_at_utc",
            "resolved_watch_url",
            "resolved_video_id",
            "resolved_source_type",
            "resolution_run_id",
            "resolution_notes",
        ]
    )


def _load_current_queue(dataset_root: Path) -> pd.DataFrame:
    path = _current_queue_path(dataset_root)
    if not path.exists():
        return _empty_queue_frame()
    frame = pd.read_parquet(path)
    if frame.empty:
        return _empty_queue_frame()
    return frame.copy()


def _active_queue_index(current_queue: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if current_queue.empty:
        return {}
    deduped = current_queue.drop_duplicates(subset=["yt_key"], keep="last")
    out: dict[str, dict[str, Any]] = {}
    for row in deduped.to_dict(orient="records"):
        yt_key = _clean_text(row.get("yt_key"))
        if yt_key:
            out[yt_key] = dict(row)
    return out


def _new_queue_id() -> str:
    return f"queue_{uuid.uuid4().hex[:12]}"


def _problem_type(row: dict[str, Any]) -> str:
    problem_type = _clean_text(row.get("problem_type"))
    if problem_type:
        return problem_type
    if _as_bool(row.get("unresolved_flag")):
        return "missing_youtube_link"
    if _as_bool(row.get("playability_mismatch_flag")):
        return "unplayable_youtube_link"
    if _as_bool(row.get("duplicate_conflict_flag")):
        return "duplicate_conflict"
    return "validation_mismatch"


def _problem_reason(row: dict[str, Any], problem_type: str) -> str:
    reason = _clean_text(row.get("problem_reason"))
    if reason:
        return reason
    return problem_type


def _resolution_fields(row: dict[str, Any]) -> tuple[str, str, str]:
    replacement_valid = _as_bool(row.get("replacement_valid_flag"))
    if replacement_valid:
        return (
            _clean_text(row.get("candidate_watch_url")),
            _clean_text(row.get("candidate_video_id")),
            _clean_text(row.get("candidate_source_type")) or "resolved",
        )
    return (
        _clean_text(row.get("current_watch_url")),
        _clean_text(row.get("current_video_id")),
        _clean_text(row.get("current_source_type")) or "direct",
    )


def _build_queue_record(
    row: dict[str, Any],
    existing: dict[str, Any] | None,
    now_iso: str,
    run_id: str,
) -> tuple[dict[str, Any] | None, str]:
    validation_outcome = _clean_text(row.get("validation_outcome")).lower()
    yt_key = _clean_text(row.get("yt_key"))
    if not yt_key:
        return None, "skipped"

    if validation_outcome == "auto_pass":
        if not existing:
            return None, "skipped"
        resolved_watch_url, resolved_video_id, resolved_source_type = _resolution_fields(row)
        updated = dict(existing)
        updated.update(
            {
                "artist": _clean_text(row.get("artist")) or _clean_text(existing.get("artist")),
                "track": _clean_text(row.get("track")) or _clean_text(existing.get("track")),
                "version_tag": _clean_text(row.get("version_tag")) or _clean_text(existing.get("version_tag")),
                "dataset_snapshot_id": _clean_text(row.get("dataset_snapshot_id")) or _clean_text(existing.get("dataset_snapshot_id")),
                "queue_status": "resolved",
                "problem_type": "",
                "problem_reason": "",
                "current_watch_url": _clean_text(row.get("current_watch_url")),
                "current_video_id": _clean_text(row.get("current_video_id")),
                "current_source_type": _clean_text(row.get("current_source_type")),
                "last_seen_at_utc": _clean_text(row.get("validated_at_utc")) or now_iso,
                "candidate_dataset_version": _clean_text(row.get("candidate_dataset_version")),
                "base_dataset_version": _clean_text(row.get("base_dataset_version")),
                "row_confidence_band": _clean_text(row.get("row_confidence_band")) or "unknown",
                "golden_song_flag": _as_bool(row.get("golden_song_flag")),
                "manual_review_required": False,
                "resolved_at_utc": _clean_text(row.get("validated_at_utc")) or now_iso,
                "resolved_watch_url": resolved_watch_url,
                "resolved_video_id": resolved_video_id,
                "resolved_source_type": resolved_source_type,
                "resolution_run_id": _clean_text(row.get("validation_run_id")) or run_id,
                "resolution_notes": "validated_auto_pass",
                "last_error_code": _clean_text(row.get("error_code")),
            }
        )
        return updated, "resolved"

    queue_status = "manual_review" if validation_outcome == "manual_review" else "open"
    first_seen = _clean_text(existing.get("first_seen_at_utc")) if existing else ""
    record = dict(existing or {})
    record.update(
        {
            "queue_id": _clean_text(record.get("queue_id")) or _new_queue_id(),
            "yt_key": yt_key,
            "artist": _clean_text(row.get("artist")),
            "track": _clean_text(row.get("track")),
            "version_tag": _clean_text(row.get("version_tag")) or "default",
            "dataset_snapshot_id": _clean_text(row.get("dataset_snapshot_id")),
            "queue_status": queue_status,
            "problem_type": _problem_type(row),
            "problem_reason": _problem_reason(row, _problem_type(row)),
            "current_watch_url": _clean_text(row.get("current_watch_url")),
            "current_video_id": _clean_text(row.get("current_video_id")),
            "current_source_type": _clean_text(row.get("current_source_type")) or "unresolved",
            "first_seen_at_utc": first_seen or (_clean_text(row.get("validated_at_utc")) or now_iso),
            "last_seen_at_utc": _clean_text(row.get("validated_at_utc")) or now_iso,
            "last_attempted_at_utc": _clean_text(record.get("last_attempted_at_utc")),
            "attempt_count": int(record.get("attempt_count", 0) or 0),
            "last_attempt_outcome": _clean_text(record.get("last_attempt_outcome")),
            "last_error_code": _clean_text(row.get("error_code")),
            "candidate_dataset_version": _clean_text(row.get("candidate_dataset_version")),
            "base_dataset_version": _clean_text(row.get("base_dataset_version")),
            "row_confidence_band": _clean_text(row.get("row_confidence_band")) or "unknown",
            "golden_song_flag": _as_bool(row.get("golden_song_flag")),
            "manual_review_required": _as_bool(row.get("manual_review_required")) or queue_status == "manual_review",
            "resolved_at_utc": "",
            "resolved_watch_url": "",
            "resolved_video_id": "",
            "resolved_source_type": "",
            "resolution_run_id": "",
            "resolution_notes": "",
        }
    )
    if existing:
        return record, "updated"
    return record, "created"


def update_unresolved_problem_queue(
    validation_rows: pd.DataFrame,
    dataset_root: Path,
    run_id: str,
    candidate_dataset_version: str,
    base_dataset_version: str,
) -> dict[str, Any]:
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    current_queue = _load_current_queue(dataset_root)
    current_by_key = _active_queue_index(current_queue)
    touched_keys: set[str] = set()
    merged_records: list[dict[str, Any]] = []
    created_count = 0
    updated_count = 0
    resolved_count = 0

    for row in validation_rows.to_dict(orient="records"):
        yt_key = _clean_text(row.get("yt_key"))
        if not yt_key:
            continue
        existing = current_by_key.get(yt_key)
        record, action = _build_queue_record(row=row, existing=existing, now_iso=now_iso, run_id=run_id)
        touched_keys.add(yt_key)
        if record is not None:
            merged_records.append(record)
        if action == "created":
            created_count += 1
        elif action == "updated":
            updated_count += 1
        elif action == "resolved":
            resolved_count += 1

    for yt_key, existing in current_by_key.items():
        if yt_key not in touched_keys:
            merged_records.append(dict(existing))

    merged_frame = pd.DataFrame(merged_records)
    if merged_frame.empty:
        merged_frame = _empty_queue_frame()
    else:
        merged_frame = merged_frame.drop_duplicates(subset=["yt_key"], keep="last").sort_values(
            by=["queue_status", "artist", "track"], na_position="last"
        )

    status_counts = Counter(str(value or "").strip() for value in merged_frame.get("queue_status", pd.Series(dtype=str)).tolist())
    active_problem_types = Counter(
        str(value or "").strip()
        for value in merged_frame.loc[
            merged_frame.get("queue_status", pd.Series(dtype=str)).isin(["open", "manual_review", "retry_pending"]),
            "problem_type",
        ].tolist()
        if str(value or "").strip()
    )

    current_queue_path = _current_queue_path(dataset_root)
    current_summary_path = _current_summary_path(dataset_root)
    history_dir = _history_dir(dataset_root, now_dt)
    history_queue_path = history_dir / f"{run_id}.parquet"
    history_summary_path = history_dir / f"{run_id}.summary.json"

    summary = {
        "run_id": run_id,
        "updated_at_utc": now_iso,
        "current_queue_uri": str(current_queue_path.resolve()),
        "open_count": int(status_counts.get("open", 0)),
        "retry_pending_count": int(status_counts.get("retry_pending", 0)),
        "resolved_count": int(status_counts.get("resolved", 0)),
        "manual_review_count": int(status_counts.get("manual_review", 0)),
        "archived_count": int(status_counts.get("archived", 0)),
        "new_items_added": int(created_count),
        "items_retried": 0,
        "items_resolved": int(resolved_count),
        "existing_items_updated": int(updated_count),
        "budget_blocked_count": int(active_problem_types.get("resolver_budget_blocked", 0)),
        "top_problem_types": dict(active_problem_types.most_common(10)),
        "candidate_dataset_version": str(candidate_dataset_version or "").strip(),
        "base_dataset_version": str(base_dataset_version or "").strip(),
        "current_row_count": int(len(merged_frame)),
        "history_queue_uri": str(history_queue_path.resolve()),
        "history_summary_uri": str(history_summary_path.resolve()),
    }

    _write_parquet(current_queue_path, merged_frame)
    _write_json(current_summary_path, summary)
    _write_parquet(history_queue_path, merged_frame)
    _write_json(history_summary_path, summary)

    return {
        "current_queue_path": str(current_queue_path.resolve()),
        "current_summary_path": str(current_summary_path.resolve()),
        "history_queue_path": str(history_queue_path.resolve()),
        "history_summary_path": str(history_summary_path.resolve()),
        "summary": summary,
        "row_count": int(len(merged_frame)),
    }
