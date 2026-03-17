from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pandas as pd

import phase2_runtime_config as cfg
from phase2_managed_loader import load_prepared_dataset, release_summary


PROBLEM_STATUSES = {
    "unplayable_resolver_returned_unplayable",
    "unplayable_no_resolver_match",
}


def _as_file_uri(path: Path) -> str:
    return f"file://{path.resolve()}"


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _schema_hash(df: pd.DataFrame) -> str:
    schema = [{"name": str(col), "dtype": str(dtype)} for col, dtype in zip(df.columns, df.dtypes)]
    payload = json.dumps(schema, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_phase2_test_module() -> Any:
    test_dir = cfg.REPO_ROOT / "recommendation_app" / "go-live" / "code" / "test"
    if str(test_dir) not in sys.path:
        sys.path.insert(0, str(test_dir))
    import application_phase2 as phase2_test_app  # type: ignore

    phase2_test_app.patch_base_app_for_phase2()
    return phase2_test_app


def _first_nonempty_text(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, float) and pd.isna(value):
            continue
        text = str(value).strip()
        if text and text.lower() != "nan":
            return text
    return ""


def _path_from_text(value: object) -> Path:
    text = str(value or "").strip()
    if not text:
        return Path("")
    if text.startswith("file://"):
        parsed = urlparse(text)
        return Path(unquote(parsed.path)).expanduser()
    return Path(text).expanduser()


def _load_latest_report(dataset_root: Path, release_version: str) -> tuple[pd.DataFrame, Path]:
    report_path = dataset_root / "snapshots" / release_version / "youtube_link_revalidation_report.csv"
    if report_path.exists():
        return pd.read_csv(report_path), report_path.resolve()

    metadata_path = dataset_root / "snapshots" / release_version / "metadata.json"
    if metadata_path.exists():
        metadata = _read_json(metadata_path)
        pq_meta = metadata.get("problem_queue_revalidation") or {}
        input_report = _path_from_text(pq_meta.get("input_report", ""))
        if input_report.exists():
            return pd.read_csv(input_report), input_report.resolve()
        source_meta = metadata.get("source") or {}
        parent_version = str(source_meta.get("parent_dataset_version", "") or "").strip()
        if parent_version:
            parent_report = dataset_root / "snapshots" / parent_version / "youtube_link_revalidation_report.csv"
            if parent_report.exists():
                return pd.read_csv(parent_report), parent_report.resolve()

    raise FileNotFoundError(f"Revalidation report not found for release {release_version}")


def revalidate_problem_queue(
    dry_run: bool = False,
    workers: int = 16,
    max_problem_rows: int = 0,
    max_resolve_keys: int = 0,
) -> dict[str, Any]:
    dataset_root = cfg.dataset_root()
    current_df, current_release = load_prepared_dataset(dataset_root=dataset_root)
    current_meta = release_summary(current_release, dataset_root=dataset_root)
    report_df, input_report_path = _load_latest_report(dataset_root, current_release.version)
    if "status" not in report_df.columns:
        raise ValueError("Revalidation report is missing required column: status")
    problem_df = report_df[report_df["status"].astype(str).isin(PROBLEM_STATUSES)].copy()
    total_problem_rows = int(len(problem_df))
    if max_problem_rows > 0 and len(problem_df) > int(max_problem_rows):
        problem_df = problem_df.head(int(max_problem_rows)).copy()

    phase2_test_app = _load_phase2_test_module()
    base_app = phase2_test_app.base_app
    df = current_df.copy()

    try:
        import youtube_live_resolver as yt_resolver  # type: ignore
    except ModuleNotFoundError:
        yt_resolver = None  # type: ignore[assignment]

    rows_examined = 0
    rows_selected = int(len(problem_df))
    rows_direct_now_playable = 0
    rows_replaced = 0
    rows_still_unresolved = 0
    rows_skipped_by_bounded_key = 0
    resolver_error_keys = 0
    audit_rows: list[dict[str, Any]] = []
    row_states: list[dict[str, Any]] = []
    resolver_keys_to_fetch: set[tuple[str, str, str]] = set()

    for _pos, item in problem_df.iterrows():
        rows_examined += 1
        if rows_examined % 250 == 0 or rows_examined == len(problem_df):
            print(f"[problem-queue] profiled {rows_examined}/{len(problem_df)}")

        key = str(item.get("canonical_key", "") or "")
        artist = str(item.get("artist", "") or "")
        track = str(item.get("track", "") or "")
        status_before = str(item.get("status", "") or "")
        matches = df.index[df["canonical_key"].astype(str) == key].tolist()
        if not matches:
            row_states.append(
                {
                    "canonical_key": key,
                    "artist": artist,
                    "track": track,
                    "status_before": status_before,
                    "status_after": "missing_from_snapshot",
                    "old_watch_url": str(item.get("old_watch_url", "") or ""),
                    "new_watch_url": "",
                    "resolver_reason": "canonical_key not found in current snapshot",
                    "idx": None,
                    "needs_resolve": False,
                }
            )
            continue

        idx = matches[0]
        row = df.loc[idx]
        credits = str(row.get("artist_credits", "") or "")
        direct_watch = base_app.normalize_youtube_watch_url(
            _first_nonempty_text(row.get("youtube_link"), row.get("url_youtube"))
        )
        direct_id = base_app.extract_youtube_video_id(direct_watch) if direct_watch else None
        direct_ok = bool(direct_id and phase2_test_app._is_video_playlist_playable_phase2(str(direct_id), timeout=6))
        resolver_key = (artist, track, credits)
        if not direct_ok:
            resolver_keys_to_fetch.add(resolver_key)

        row_states.append(
            {
                "canonical_key": key,
                "artist": artist,
                "track": track,
                "status_before": status_before,
                "old_watch_url": str(direct_watch or ""),
                "new_watch_url": "",
                "resolver_reason": "",
                "idx": idx,
                "direct_ok": direct_ok,
                "direct_id": str(direct_id or ""),
                "resolver_key": resolver_key,
                "needs_resolve": not direct_ok,
            }
        )

    resolver_results: dict[tuple[str, str, str], tuple[str, str, float, bool]] = {}
    resolver_keys = sorted(resolver_keys_to_fetch)
    skipped_resolver_keys = 0
    allowed_resolver_keys: set[tuple[str, str, str]] | None = None
    if max_resolve_keys > 0 and len(resolver_keys) > int(max_resolve_keys):
        allowed_resolver_keys = set(resolver_keys[: int(max_resolve_keys)])
        skipped_resolver_keys = len(resolver_keys) - len(allowed_resolver_keys)
        resolver_keys = sorted(allowed_resolver_keys)
    if resolver_keys:
        print(f"[problem-queue] resolving {len(resolver_keys)} unique keys")

        def _resolve_key(resolver_key: tuple[str, str, str]) -> tuple[tuple[str, str, str], tuple[str, str, float, bool], bool]:
            artist, track, credits = resolver_key
            if yt_resolver is not None:
                try:
                    cache_key = yt_resolver._cache_key(artist, track)  # type: ignore[union-attr]
                    conn = yt_resolver._cache_connect()  # type: ignore[union-attr]
                    conn.execute("DELETE FROM youtube_resolver_cache WHERE yt_key = ?", (cache_key,))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
            try:
                new_watch, resolver_reason, score = phase2_test_app.resolve_live_youtube_watch_url_phase2(
                    artist=artist,
                    track=track,
                    artist_credits_json=credits,
                    timeout=8,
                )
                new_watch = str(new_watch or "").strip()
                new_id = base_app.extract_youtube_video_id(new_watch) if new_watch else None
                new_ok = bool(new_id and phase2_test_app._is_video_playlist_playable_phase2(str(new_id), timeout=6))
                return resolver_key, (new_watch, str(resolver_reason or ""), float(score or 0.0), new_ok), False
            except Exception as exc:
                return resolver_key, ("", str(exc), 0.0, False), True

        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
            futures = [executor.submit(_resolve_key, resolver_key) for resolver_key in resolver_keys]
            total = len(futures)
            done = 0
            for future in as_completed(futures):
                resolver_key, payload, is_error = future.result()
                resolver_results[resolver_key] = payload
                if is_error:
                    resolver_error_keys += 1
                done += 1
                if done % 250 == 0 or done == total:
                    print(f"[problem-queue] resolved {done}/{total}")

    for pos, state in enumerate(row_states, start=1):
        if pos % 250 == 0 or pos == len(row_states):
            print(f"[problem-queue] applied {pos}/{len(row_states)}")

        if state.get("status_after") == "missing_from_snapshot":
            rows_still_unresolved += 1
            audit_rows.append(state)
            continue

        key = str(state["canonical_key"])
        artist = str(state["artist"])
        track = str(state["track"])
        status_before = str(state["status_before"])
        direct_watch = str(state["old_watch_url"])
        idx = state.get("idx")

        if bool(state.get("direct_ok")):
            rows_direct_now_playable += 1
            audit_rows.append(
                {
                    "canonical_key": key,
                    "artist": artist,
                    "track": track,
                    "status_before": status_before,
                    "status_after": "direct_now_playable",
                    "old_watch_url": direct_watch,
                    "new_watch_url": direct_watch,
                    "resolver_reason": "current dataset direct watch URL passes runtime playability check",
                }
            )
            continue

        resolver_key = state["resolver_key"]
        if allowed_resolver_keys is not None and resolver_key not in allowed_resolver_keys:
            rows_still_unresolved += 1
            rows_skipped_by_bounded_key += 1
            audit_rows.append(
                {
                    "canonical_key": key,
                    "artist": artist,
                    "track": track,
                    "status_before": status_before,
                    "status_after": "still_unresolved",
                    "old_watch_url": direct_watch,
                    "new_watch_url": "",
                    "resolver_reason": "bounded_mode: resolver key skipped by max_resolve_keys",
                }
            )
            continue

        new_watch, resolver_reason, _score, new_ok = resolver_results.get(resolver_key, ("", "no_result", 0.0, False))
        new_watch = str(new_watch or "").strip()

        if not new_ok:
            rows_still_unresolved += 1
            audit_rows.append(
                {
                    "canonical_key": key,
                    "artist": artist,
                    "track": track,
                    "status_before": status_before,
                    "status_after": "still_unresolved",
                    "old_watch_url": direct_watch,
                    "new_watch_url": new_watch,
                    "resolver_reason": str(resolver_reason or ""),
                }
            )
            continue

        new_id = base_app.extract_youtube_video_id(new_watch) if new_watch else None
        if idx is not None and str(new_watch) != direct_watch:
            df.at[idx, "youtube_link"] = str(new_watch)
            if "url_youtube" in df.columns:
                df.at[idx, "url_youtube"] = str(new_watch)
        if idx is not None and "youtube_video_id" in df.columns and new_id:
            df.at[idx, "youtube_video_id"] = str(new_id)

        rows_replaced += 1
        audit_rows.append(
            {
                "canonical_key": key,
                "artist": artist,
                "track": track,
                "status_before": status_before,
                "status_after": "replaced_by_tightened_resolver",
                "old_watch_url": direct_watch,
                "new_watch_url": str(new_watch or ""),
                "resolver_reason": str(resolver_reason or ""),
            }
        )

    summary = {
        "problem_rows_total": int(total_problem_rows),
        "problem_rows_selected": int(rows_selected),
        "problem_rows_examined": int(rows_examined),
        "rows_direct_now_playable": int(rows_direct_now_playable),
        "rows_replaced": int(rows_replaced),
        "rows_still_unresolved": int(rows_still_unresolved),
        "rows_skipped_by_bounded_key": int(rows_skipped_by_bounded_key),
        "resolver_keys_skipped_by_bounded_limit": int(skipped_resolver_keys),
        "resolver_error_keys": int(resolver_error_keys),
        "bounded_mode": bool(max_problem_rows > 0 or max_resolve_keys > 0),
        "max_problem_rows": int(max_problem_rows),
        "max_resolve_keys": int(max_resolve_keys),
    }

    if dry_run:
        return {"status": "dry_run", "summary": summary}

    if rows_replaced <= 0:
        return {
            "status": "no_changes",
            "summary": summary,
            "parent_dataset_version": current_release.version,
        }

    now = datetime.now(timezone.utc)
    version = now.strftime("ds_%Y%m%d_%H%M%S_utc")
    snapshot_dir = dataset_root / "snapshots" / version
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    artifact_paths: dict[str, Path] = {}
    if cfg.PUBLISH_INCLUDE_PARQUET:
        parquet_path = snapshot_dir / "prepared.parquet"
        try:
            df.to_parquet(parquet_path, index=False)
            artifact_paths["parquet"] = parquet_path
        except Exception:
            pass
    if cfg.PUBLISH_INCLUDE_CSV:
        csv_path = snapshot_dir / "prepared.csv"
        df.to_csv(csv_path, index=False)
        artifact_paths["csv"] = csv_path
    if not artifact_paths:
        raise ValueError("At least one artifact format must be enabled (CSV or Parquet).")

    preferred_artifact = artifact_paths.get("parquet") or artifact_paths.get("csv")
    assert preferred_artifact is not None

    audit_df = pd.DataFrame(audit_rows)
    audit_path = snapshot_dir / "youtube_problem_queue_revalidation_report.csv"
    audit_df.to_csv(audit_path, index=False)
    # Carry forward the canonical link-revalidation input report so future runs
    # can always resolve the problem queue source chain from the current snapshot.
    link_report_path = snapshot_dir / "youtube_link_revalidation_report.csv"
    report_df.to_csv(link_report_path, index=False)

    metadata = {
        "dataset_version": version,
        "created_at_utc": now.isoformat(),
        "row_count": int(len(df)),
        "schema_hash_sha256": _schema_hash(df),
        "prepared_uri": _as_file_uri(preferred_artifact),
        "artifacts": {
            name: {
                "path": str(path.resolve()),
                "sha256": _sha256_file(path),
                "size_bytes": int(path.stat().st_size),
            }
            for name, path in artifact_paths.items()
        },
        "source": {
            "dataset_source": "managed_snapshot_problem_queue_revalidation",
            "parent_dataset_version": current_release.version,
            "parent_metadata_path": str(current_release.metadata_path.resolve()),
            "parent_release_summary": current_meta,
        },
        "problem_queue_revalidation": summary
        | {
            "input_report": str(input_report_path.resolve()),
            "input_statuses": sorted(PROBLEM_STATUSES),
            "report_path": str(audit_path.resolve()),
            "input_report_carried_forward_path": str(link_report_path.resolve()),
            "resolver_module": "go-live/code/test/application_phase2.py",
            "selection_mode": "tightened_scorer_plus_runtime_playability",
        },
    }

    metadata_path = snapshot_dir / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    latest_payload = {
        "dataset_version": version,
        "updated_at_utc": now.isoformat(),
        "metadata_uri": _as_file_uri(metadata_path),
    }
    pointer_path = dataset_root / cfg.DATASET_POINTER_FILE
    pointer_path.write_text(json.dumps(latest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "status": "ok",
        "dataset_version": version,
        "metadata_path": str(metadata_path.resolve()),
        "pointer_path": str(pointer_path.resolve()),
        "report_path": str(audit_path.resolve()),
        "summary": summary,
    }


if __name__ == "__main__":
    dry_run = False
    workers = 16
    max_problem_rows = 0
    max_resolve_keys = 0
    for arg in sys.argv[1:]:
        text = str(arg).strip()
        if text == "--dry-run":
            dry_run = True
        elif text.startswith("--workers="):
            try:
                workers = int(text.split("=", 1)[1])
            except Exception:
                workers = 16
        elif text.startswith("--max-problem-rows="):
            try:
                max_problem_rows = int(text.split("=", 1)[1])
            except Exception:
                max_problem_rows = 0
        elif text.startswith("--max-resolve-keys="):
            try:
                max_resolve_keys = int(text.split("=", 1)[1])
            except Exception:
                max_resolve_keys = 0
    print(
        json.dumps(
            revalidate_problem_queue(
                dry_run=dry_run,
                workers=workers,
                max_problem_rows=max_problem_rows,
                max_resolve_keys=max_resolve_keys,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
