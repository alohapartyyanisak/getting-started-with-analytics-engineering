from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

import phase2_runtime_config as cfg
from phase2_managed_loader import REQUIRED_PREPARED_COLUMNS, load_prepared_dataset, resolve_release


REQUIRED_METADATA_KEYS = {
    "dataset_version",
    "created_at_utc",
    "row_count",
    "schema_hash_sha256",
    "prepared_uri",
    "artifacts",
    "source",
}

TRUSTED_REASON_KEYWORDS = (
    "official",
    "vevo",
    "topic",
    "records",
    "music",
    "artist",
    "channel",
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean_text(value: object) -> str:
    text = str(value or "").strip()
    return "" if text.lower() == "nan" else text


def _snapshot_dir(version: str, dataset_root: Path) -> Path:
    return dataset_root / "snapshots" / version


def _resolve_storage_path(path_text: str, snapshot_dir: Path) -> Path:
    text = str(path_text or "").strip()
    if not text:
        return Path("")
    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        return candidate

    dataset_root = snapshot_dir.parent.parent
    dataset_candidate = (dataset_root / candidate).resolve()
    if dataset_candidate.exists():
        return dataset_candidate

    snapshot_candidate = (snapshot_dir / candidate).resolve()
    if snapshot_candidate.exists():
        return snapshot_candidate

    return dataset_candidate


def _extract_video_id(watch_url: object, explicit_video_id: object) -> str:
    direct = str(explicit_video_id or "").strip()
    if direct and direct.lower() != "nan":
        return direct
    text = str(watch_url or "").strip()
    if not text or text.lower() == "nan":
        return ""
    match = re.search(r"(?:v=|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})", text)
    return match.group(1) if match else ""


def _dataset_duplicate_ratio(df: pd.DataFrame) -> float:
    ids = [_extract_video_id(w, v) for w, v in zip(df.get("youtube_link", []), df.get("youtube_video_id", []))]
    ids = [item for item in ids if item]
    if not ids:
        return 0.0
    counts = Counter(ids)
    duplicate_rows = sum(count - 1 for count in counts.values() if count > 1)
    return duplicate_rows / float(len(ids))


def _load_report(snapshot_dir: Path, metadata: dict[str, Any]) -> tuple[str, pd.DataFrame | None, str | None]:
    if "problem_queue_revalidation" in metadata:
        section = metadata["problem_queue_revalidation"] or {}
        path = str(section.get("report_path", "")).strip()
        report_type = "problem_queue_revalidation"
    elif "revalidation" in metadata:
        section = metadata["revalidation"] or {}
        path = str(section.get("report_path", "")).strip()
        report_type = "revalidation"
    else:
        return "none", None, None

    report_path = _resolve_storage_path(path, snapshot_dir) if path else None
    if report_path is None or not report_path.exists():
        local_candidates = {
            "problem_queue_revalidation": snapshot_dir / "youtube_problem_queue_revalidation_report.csv",
            "revalidation": snapshot_dir / "youtube_link_revalidation_report.csv",
        }
        report_path = local_candidates.get(report_type)
    if report_path is None or not report_path.exists():
        return report_type, None, None
    return report_type, pd.read_csv(report_path), str(report_path.resolve())


def _is_trusted_reason(reason: object) -> bool:
    text = str(reason or "").strip().lower()
    if not text:
        return False
    return any(keyword in text for keyword in TRUSTED_REASON_KEYWORDS)


def _triage_report(report_type: str, report_df: pd.DataFrame | None) -> tuple[int, int, int]:
    if report_df is None or report_df.empty:
        return 0, 0, 0

    auto_pass = 0
    manual_review = 0
    auto_reject = 0

    if report_type == "revalidation":
        for _, row in report_df.iterrows():
            status = str(row.get("status", "") or "")
            reason = row.get("resolver_reason", "")
            if status in {"kept_direct_playable", "no_direct_id", "unplayable_resolved_same_id"}:
                auto_pass += 1
            elif status == "unplayable_replaced_by_resolver":
                if _is_trusted_reason(reason):
                    auto_pass += 1
                else:
                    manual_review += 1
            else:
                auto_reject += 1
        return auto_pass, manual_review, auto_reject

    if report_type == "problem_queue_revalidation":
        for _, row in report_df.iterrows():
            status = str(row.get("status_after", "") or "")
            reason = row.get("resolver_reason", "")
            if status == "direct_now_playable":
                auto_pass += 1
            elif status == "replaced_by_tightened_resolver":
                if _is_trusted_reason(reason):
                    auto_pass += 1
                else:
                    manual_review += 1
            elif status in {"still_unresolved", "missing_from_snapshot"}:
                auto_reject += 1
            else:
                manual_review += 1
        return auto_pass, manual_review, auto_reject

    return 0, 0, 0


def _infer_version_tag(track: object) -> str:
    text = str(track or "").strip().lower()
    if not text:
        return "default"
    tags: list[str] = []
    if "live" in text:
        tags.append("live")
    if "acoustic" in text:
        tags.append("acoustic")
    if "remix" in text:
        tags.append("remix")
    if not tags:
        return "default"
    return "+".join(sorted(set(tags)))


def _report_row_status(report_type: str, row: pd.Series) -> str:
    if report_type == "problem_queue_revalidation":
        return str(row.get("status_after", "") or "").strip()
    return str(row.get("status", "") or "").strip()


def _report_row_problem_type(status: str, reason: object) -> str | None:
    text = str(status or "").strip().lower()
    reason_text = str(reason or "").strip().lower()
    if not text and not reason_text:
        return None
    if "duplicate" in text or "duplicate" in reason_text:
        return "duplicate_conflict"
    if "budget" in text or "quota" in text or "budget" in reason_text or "quota" in reason_text:
        return "resolver_budget_blocked"
    if "missing" in text:
        return "missing_youtube_link"
    if "low_confidence" in text or "low confidence" in reason_text:
        return "low_confidence_resolve"
    if "manual_override" in text:
        return None
    if "unplayable" in text:
        return "unplayable_youtube_link"
    if "unresolved" in text:
        return "missing_youtube_link"
    return "validation_mismatch"


def _report_row_validation_outcome(report_type: str, row: pd.Series) -> str:
    status = _report_row_status(report_type, row)
    reason = row.get("resolver_reason", "")
    if report_type == "revalidation":
        if status in {"kept_direct_playable", "no_direct_id", "unplayable_resolved_same_id"}:
            return "auto_pass"
        if status == "unplayable_replaced_by_resolver":
            return "auto_pass" if _is_trusted_reason(reason) else "manual_review"
        return "auto_reject"

    if report_type == "problem_queue_revalidation":
        if status in {"direct_now_playable", "manual_override_applied"}:
            return "auto_pass"
        if status == "replaced_by_tightened_resolver":
            return "auto_pass" if _is_trusted_reason(reason) else "manual_review"
        if status in {"still_unresolved", "missing_from_snapshot"}:
            return "auto_reject"
        return "manual_review"

    return "auto_reject"


def _report_row_behavioral_outcome(validation_outcome: str) -> str:
    if validation_outcome == "auto_pass":
        return "pass"
    if validation_outcome == "manual_review":
        return "manual_review"
    return "fail"


def _candidate_watch_url(report_type: str, row: pd.Series) -> str:
    new_watch = _clean_text(row.get("new_watch_url", ""))
    old_watch = _clean_text(row.get("old_watch_url", ""))
    if new_watch:
        return new_watch
    if report_type == "revalidation" and str(row.get("status", "") or "").strip() in {
        "kept_direct_playable",
        "no_direct_id",
        "unplayable_resolved_same_id",
    }:
        return old_watch
    if report_type == "problem_queue_revalidation" and str(row.get("status_after", "") or "").strip() in {
        "direct_now_playable",
    }:
        return old_watch
    return old_watch if old_watch and not new_watch else ""


def _source_type_from_watch(watch_url: str, *, status: str, reason: object, unresolved_fallback: bool = True) -> str:
    text = str(status or "").strip().lower()
    reason_text = str(reason or "").strip().lower()
    watch = _clean_text(watch_url)
    if not watch:
        return "unresolved" if unresolved_fallback else ""
    if "resolver" in text or "manual_override" in text or _is_trusted_reason(reason_text):
        return "resolved"
    return "direct"


def _row_confidence_band(validation_outcome: str, reason: object) -> str:
    if validation_outcome == "auto_pass":
        return "high" if _is_trusted_reason(reason) else "medium"
    if validation_outcome == "manual_review":
        return "medium"
    return "unknown"


def _row_error_code(problem_type: str | None, *, unresolved_flag: bool, playability_mismatch_flag: bool) -> str:
    if problem_type == "duplicate_conflict":
        return "DUPLICATE_LINK_CONFLICT"
    if problem_type == "resolver_budget_blocked":
        return "RESOLVER_BUDGET_BLOCKED"
    if problem_type == "low_confidence_resolve":
        return "LOW_CONFIDENCE_RESOLVE"
    if playability_mismatch_flag:
        return "UNPLAYABLE_YOUTUBE_LINK"
    if unresolved_flag or problem_type == "missing_youtube_link":
        return "UNRESOLVED_YOUTUBE_LINK"
    if problem_type == "validation_mismatch":
        return "VALIDATION_MISMATCH"
    return ""


def _report_rows_to_validation_rows(
    *,
    report_type: str,
    report_df: pd.DataFrame | None,
    candidate_version: str,
    base_version: str,
    validation_run_id: str,
    validated_at_utc: str,
    execution_mode: str,
) -> pd.DataFrame:
    columns = [
        "artist",
        "track",
        "yt_key",
        "canonical_key",
        "version_tag",
        "dataset_snapshot_id",
        "candidate_dataset_version",
        "base_dataset_version",
        "current_watch_url",
        "current_video_id",
        "current_source_type",
        "validation_outcome",
        "problem_type",
        "problem_reason",
        "behavioral_outcome",
        "row_confidence_band",
        "golden_song_flag",
        "duplicate_conflict_flag",
        "unresolved_flag",
        "playability_mismatch_flag",
        "manual_review_required",
        "candidate_watch_url",
        "candidate_video_id",
        "candidate_source_type",
        "replacement_changed_flag",
        "replacement_valid_flag",
        "validation_stage",
        "error_code",
        "notes",
        "validation_run_id",
        "validated_at_utc",
        "execution_mode",
    ]
    if report_df is None or report_df.empty:
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, Any]] = []
    for _, row in report_df.iterrows():
        status = _report_row_status(report_type, row)
        reason = row.get("resolver_reason", "")
        validation_outcome = _report_row_validation_outcome(report_type, row)
        behavioral_outcome = _report_row_behavioral_outcome(validation_outcome)
        old_watch = _clean_text(row.get("old_watch_url", ""))
        new_watch = str(_candidate_watch_url(report_type, row) or "").strip()
        old_video = _extract_video_id(old_watch, row.get("old_video_id", ""))
        new_video = _extract_video_id(new_watch, row.get("new_video_id", ""))
        replacement_changed = bool(new_watch and new_watch != old_watch)
        problem_type = _report_row_problem_type(status, reason)
        duplicate_conflict_flag = bool(problem_type == "duplicate_conflict")
        unresolved_flag = bool(not new_watch or status in {"still_unresolved", "missing_from_snapshot"})
        playability_mismatch_flag = "unplayable" in str(status or "").strip().lower()
        manual_review_required = bool(validation_outcome == "manual_review")
        candidate_source_type = _source_type_from_watch(new_watch, status=status, reason=reason)
        current_source_type = _source_type_from_watch(old_watch, status=status, reason=reason)
        rows.append(
            {
                "artist": str(row.get("artist", "") or "").strip(),
                "track": str(row.get("track", "") or "").strip(),
                "yt_key": str(row.get("canonical_key", "") or "").strip(),
                "canonical_key": str(row.get("canonical_key", "") or "").strip(),
                "version_tag": _infer_version_tag(row.get("track", "")),
                "dataset_snapshot_id": candidate_version,
                "candidate_dataset_version": candidate_version,
                "base_dataset_version": base_version,
                "current_watch_url": old_watch,
                "current_video_id": old_video,
                "current_source_type": current_source_type,
                "validation_outcome": validation_outcome,
                "problem_type": problem_type,
                "problem_reason": str(reason or "").strip() or None,
                "behavioral_outcome": behavioral_outcome,
                "row_confidence_band": _row_confidence_band(validation_outcome, reason),
                "golden_song_flag": False,
                "duplicate_conflict_flag": duplicate_conflict_flag,
                "unresolved_flag": unresolved_flag,
                "playability_mismatch_flag": playability_mismatch_flag,
                "manual_review_required": manual_review_required,
                "candidate_watch_url": new_watch,
                "candidate_video_id": new_video,
                "candidate_source_type": candidate_source_type,
                "replacement_changed_flag": replacement_changed,
                "replacement_valid_flag": bool(validation_outcome == "auto_pass" and bool(new_watch)),
                "validation_stage": "quality",
                "error_code": _row_error_code(
                    problem_type,
                    unresolved_flag=unresolved_flag,
                    playability_mismatch_flag=playability_mismatch_flag,
                ),
                "notes": "",
                "validation_run_id": validation_run_id,
                "validated_at_utc": validated_at_utc,
                "execution_mode": execution_mode,
            }
        )

    return pd.DataFrame(rows, columns=columns)


def _validate_contract(metadata: dict[str, Any], snapshot_dir: Path, df: pd.DataFrame) -> tuple[bool, list[str]]:
    failures: list[str] = []
    missing_keys = sorted(REQUIRED_METADATA_KEYS - set(metadata.keys()))
    if missing_keys:
        failures.append(f"metadata missing required keys: {missing_keys}")

    artifacts = metadata.get("artifacts") or {}
    if not isinstance(artifacts, dict) or not artifacts:
        failures.append("metadata.artifacts missing or empty")
    else:
        for name, payload in artifacts.items():
            path_text = str((payload or {}).get("path", "")).strip()
            if not path_text:
                failures.append(f"artifact path missing for {name}")
                continue
            if not _resolve_storage_path(path_text, snapshot_dir).exists():
                failures.append(f"artifact path not found for {name}: {path_text}")

    if df.empty:
        failures.append("prepared dataset is empty")

    missing_columns = sorted(REQUIRED_PREPARED_COLUMNS - set(df.columns))
    if missing_columns:
        failures.append(f"prepared dataset missing required columns: {missing_columns}")

    for column in ("artist", "track"):
        if column not in df.columns:
            failures.append(f"required identity column missing: {column}")
    if "youtube_link" not in df.columns and "url_youtube" not in df.columns:
        failures.append("youtube watch URL field missing: expected youtube_link or url_youtube")

    report_type, report_df, report_path = _load_report(snapshot_dir, metadata)
    if report_type != "none" and report_df is None:
        failures.append(f"expected report for {report_type} but none was found")
    if report_path is None and report_type != "none":
        failures.append(f"report path missing for {report_type}")

    return len(failures) == 0, failures


def _revalidation_metrics(meta: dict[str, Any]) -> dict[str, Any]:
    result = {
        "rows_total": None,
        "rows_unresolved": None,
        "rows_direct_playable": None,
        "rows_replaced": None,
        "mode": "unknown",
    }
    if "revalidation" in meta:
        section = meta["revalidation"] or {}
        result.update(
            {
                "rows_total": int(section.get("rows_total", 0) or 0),
                "rows_unresolved": int(section.get("rows_unresolved", 0) or 0),
                "rows_direct_playable": int(section.get("rows_direct_playable", 0) or 0),
                "rows_replaced": int(section.get("rows_replaced", 0) or 0),
                "mode": "revalidation",
            }
        )
        return result
    if "problem_queue_revalidation" in meta:
        section = meta["problem_queue_revalidation"] or {}
        result.update(
            {
                "rows_total": int(meta.get("row_count", 0) or 0),
                "rows_unresolved": int(section.get("rows_still_unresolved", 0) or 0),
                "rows_direct_playable": int(section.get("rows_direct_now_playable", 0) or 0),
                "rows_replaced": int(section.get("rows_replaced", 0) or 0),
                "problem_rows_examined": int(section.get("problem_rows_examined", 0) or 0),
                "mode": "problem_queue_revalidation",
            }
        )
    return result


def _estimate_candidate_overall(base_meta: dict[str, Any], candidate_meta: dict[str, Any]) -> dict[str, Any]:
    base_reval = (base_meta.get("revalidation") or {}) if isinstance(base_meta.get("revalidation"), dict) else {}
    base_total = int(base_reval.get("rows_total", base_meta.get("row_count", 0)) or 0)
    has_base_revalidation = bool(base_reval)
    base_unresolved = int(base_reval.get("rows_unresolved", 0) or 0) if has_base_revalidation else None
    base_verified = (
        int(base_reval.get("rows_direct_playable", 0) or 0) + int(base_reval.get("rows_replaced", 0) or 0)
        if has_base_revalidation
        else None
    )

    if "revalidation" in candidate_meta:
        cand_reval = candidate_meta.get("revalidation") or {}
        cand_unresolved = int(cand_reval.get("rows_unresolved", 0) or 0)
        cand_verified = int(cand_reval.get("rows_direct_playable", 0) or 0) + int(cand_reval.get("rows_replaced", 0) or 0)
        return {
            "base_total": base_total,
            "base_unresolved": base_unresolved,
            "candidate_unresolved": cand_unresolved,
            "base_verified_playable": base_verified,
            "candidate_verified_playable": cand_verified,
        }

    pq = candidate_meta.get("problem_queue_revalidation") or {}
    improved = int(pq.get("rows_direct_now_playable", 0) or 0) + int(pq.get("rows_replaced", 0) or 0)
    candidate_unresolved = max(0, base_unresolved - improved) if isinstance(base_unresolved, int) else None
    candidate_verified = (base_verified + improved) if isinstance(base_verified, int) else None
    return {
        "base_total": base_total,
        "base_unresolved": base_unresolved,
        "candidate_unresolved": candidate_unresolved,
        "base_verified_playable": base_verified,
        "candidate_verified_playable": candidate_verified,
    }


def build_validation_summary(
    candidate_version: str,
    base_version: str | None = None,
    behavioral_status: str = "unknown",
    manual_status: str = "unknown",
    duplicate_worsen_tolerance: float = 0.0025,
) -> dict[str, Any]:
    dataset_root = cfg.dataset_root()
    candidate_release = resolve_release(version=candidate_version, dataset_root=dataset_root)
    candidate_df, _ = load_prepared_dataset(version=candidate_release.version, dataset_root=dataset_root)
    candidate_snapshot_dir = _snapshot_dir(candidate_release.version, dataset_root)
    candidate_meta = _read_json(candidate_release.metadata_path)

    resolved_base_version = base_version
    if not resolved_base_version:
        source = candidate_meta.get("source") or {}
        resolved_base_version = str(source.get("parent_dataset_version", "")).strip() or None
    if not resolved_base_version:
        raise ValueError("base_version is required when parent_dataset_version is not available in candidate metadata")

    base_release = resolve_release(version=resolved_base_version, dataset_root=dataset_root)
    base_df, _ = load_prepared_dataset(version=base_release.version, dataset_root=dataset_root)
    base_meta = _read_json(base_release.metadata_path)

    contract_passed, contract_failures = _validate_contract(candidate_meta, candidate_snapshot_dir, candidate_df)

    candidate_dup_ratio = _dataset_duplicate_ratio(candidate_df)
    base_dup_ratio = _dataset_duplicate_ratio(base_df)
    duplicate_ratio_delta = candidate_dup_ratio - base_dup_ratio
    duplicate_check_passed = duplicate_ratio_delta <= duplicate_worsen_tolerance

    estimates = _estimate_candidate_overall(base_meta, candidate_meta)
    base_total = int(estimates.get("base_total", 0) or 0)
    base_unresolved = estimates.get("base_unresolved")
    candidate_unresolved = estimates.get("candidate_unresolved")
    base_verified = estimates.get("base_verified_playable")
    candidate_verified = estimates.get("candidate_verified_playable")

    unresolved_delta = (
        int(candidate_unresolved) - int(base_unresolved)
        if isinstance(candidate_unresolved, int) and isinstance(base_unresolved, int)
        else None
    )
    verified_delta = (
        int(candidate_verified) - int(base_verified)
        if isinstance(candidate_verified, int) and isinstance(base_verified, int)
        else None
    )
    candidate_unresolved_ratio = (
        float(candidate_unresolved) / float(base_total)
        if isinstance(candidate_unresolved, int) and base_total
        else None
    )
    unresolved_ratio_passed = (
        (candidate_unresolved_ratio <= 0.20)
        if isinstance(candidate_unresolved_ratio, float)
        else True
    )
    if unresolved_ratio_passed and isinstance(unresolved_delta, int):
        unresolved_ratio_passed = unresolved_delta <= 0

    report_type, report_df, report_path = _load_report(candidate_snapshot_dir, candidate_meta)
    auto_pass_count, manual_review_count, auto_reject_count = _triage_report(report_type, report_df)

    quality_failures: list[str] = []
    if not duplicate_check_passed:
        quality_failures.append(
            f"dataset duplicate ratio worsened by {duplicate_ratio_delta:.4%} which exceeds tolerance {duplicate_worsen_tolerance:.4%}"
        )
    if not unresolved_ratio_passed:
        quality_failures.append(
            f"estimated unresolved ratio/regression failed: candidate_unresolved={candidate_unresolved}, base_unresolved={base_unresolved}, candidate_ratio={candidate_unresolved_ratio:.4%}"
        )
    if isinstance(verified_delta, int) and verified_delta < 0:
        quality_failures.append(
            f"estimated verified playable rows regressed: candidate={candidate_verified}, base={base_verified}"
        )
    if report_type == "none":
        quality_failures.append("candidate snapshot has no revalidation or problem-queue report metadata")

    behavioral_passed = behavioral_status == "passed"
    behavioral_failed = behavioral_status == "failed"
    if behavioral_failed:
        quality_failures.append("behavioral validation explicitly marked failed")

    quality_validation_passed = len(quality_failures) == 0
    publish_recommended = bool(
        contract_passed
        and quality_validation_passed
        and behavioral_passed
        and manual_status == "approved"
    )

    summary = {
        "candidate_dataset_version": candidate_release.version,
        "base_dataset_version": base_release.version,
        "contract_validation_passed": contract_passed,
        "quality_validation_passed": quality_validation_passed,
        "behavioral_validation_status": behavioral_status,
        "behavioral_validation_passed": behavioral_passed,
        "manual_acceptance_status": manual_status,
        "auto_pass_count": int(auto_pass_count),
        "manual_review_count": int(manual_review_count),
        "auto_reject_count": int(auto_reject_count),
        "golden_song_pass_count": 0,
        "golden_song_fail_count": 0,
        "rows_replaced_delta": int(verified_delta) if isinstance(verified_delta, int) else None,
        "rows_unresolved_delta": int(unresolved_delta) if isinstance(unresolved_delta, int) else None,
        "duplicate_ratio_base": round(base_dup_ratio, 8),
        "duplicate_ratio_candidate": round(candidate_dup_ratio, 8),
        "duplicate_ratio_delta": round(duplicate_ratio_delta, 8),
        "estimated_unresolved_ratio_candidate": round(candidate_unresolved_ratio, 8)
        if isinstance(candidate_unresolved_ratio, float)
        else None,
        "estimated_unresolved_base": int(base_unresolved) if isinstance(base_unresolved, int) else None,
        "estimated_unresolved_candidate": int(candidate_unresolved) if isinstance(candidate_unresolved, int) else None,
        "estimated_verified_playable_base": int(base_verified) if isinstance(base_verified, int) else None,
        "estimated_verified_playable_candidate": int(candidate_verified) if isinstance(candidate_verified, int) else None,
        "report_type": report_type,
        "report_path": report_path,
        "contract_failures": contract_failures,
        "quality_failures": quality_failures,
        "publish_recommended": publish_recommended,
    }
    return summary


def build_validation_rows(
    candidate_version: str,
    base_version: str | None = None,
    execution_mode: str = "unknown",
    validation_run_id: str = "",
) -> pd.DataFrame:
    dataset_root = cfg.dataset_root()
    candidate_release = resolve_release(version=candidate_version, dataset_root=dataset_root)
    candidate_snapshot_dir = _snapshot_dir(candidate_release.version, dataset_root)
    candidate_meta = _read_json(candidate_release.metadata_path)

    resolved_base_version = base_version
    if not resolved_base_version:
        source = candidate_meta.get("source") or {}
        resolved_base_version = str(source.get("parent_dataset_version", "")).strip() or None
    if not resolved_base_version:
        raise ValueError("base_version is required when parent_dataset_version is not available in candidate metadata")

    base_release = resolve_release(version=resolved_base_version, dataset_root=dataset_root)
    report_type, report_df, _report_path = _load_report(candidate_snapshot_dir, candidate_meta)
    if not validation_run_id:
        validation_run_id = f"validation_{candidate_release.version}"
    validated_at_utc = str(candidate_meta.get("created_at_utc", "") or "").strip()
    if not validated_at_utc:
        validated_at_utc = "unknown"

    return _report_rows_to_validation_rows(
        report_type=report_type,
        report_df=report_df,
        candidate_version=candidate_release.version,
        base_version=base_release.version,
        validation_run_id=validation_run_id,
        validated_at_utc=validated_at_utc,
        execution_mode=str(execution_mode or "unknown").strip() or "unknown",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build machine-readable weekly snapshot validation summary")
    parser.add_argument("--candidate-version", required=True, help="Candidate dataset version to validate")
    parser.add_argument("--base-version", default="", help="Base dataset version for comparison")
    parser.add_argument(
        "--behavioral-status",
        choices=("unknown", "passed", "failed"),
        default="unknown",
        help="Current manual/automated behavioral validation state",
    )
    parser.add_argument(
        "--manual-status",
        choices=("unknown", "approved", "rejected"),
        default="unknown",
        help="Current human acceptance state",
    )
    parser.add_argument(
        "--duplicate-worsen-tolerance",
        type=float,
        default=0.0025,
        help="Allowed absolute increase in dataset duplicate ratio before quality validation fails",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional explicit output path for the validation summary JSON",
    )
    args = parser.parse_args()

    summary = build_validation_summary(
        candidate_version=str(args.candidate_version).strip(),
        base_version=str(args.base_version).strip() or None,
        behavioral_status=str(args.behavioral_status).strip(),
        manual_status=str(args.manual_status).strip(),
        duplicate_worsen_tolerance=float(args.duplicate_worsen_tolerance),
    )

    dataset_root = cfg.dataset_root()
    output_path = Path(args.output).expanduser() if str(args.output).strip() else (
        _snapshot_dir(str(args.candidate_version).strip(), dataset_root) / "weekly_validation_summary.json"
    )
    _write_json(output_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[written] {output_path.resolve()}")


if __name__ == "__main__":
    main()
