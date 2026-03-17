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


def _snapshot_dir(version: str, dataset_root: Path) -> Path:
    return dataset_root / "snapshots" / version


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

    report_path = Path(path) if path else None
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
            if not Path(path_text).exists():
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
