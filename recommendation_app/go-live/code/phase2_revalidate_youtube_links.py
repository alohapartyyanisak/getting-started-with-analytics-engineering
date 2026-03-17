from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import phase2_runtime_config as cfg
from phase2_managed_loader import load_prepared_dataset, release_summary


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


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolver_key_to_text(resolver_key: tuple[str, str, str]) -> str:
    return "|||".join(resolver_key)


def _resolver_key_from_text(text: str) -> tuple[str, str, str]:
    parts = str(text or "").split("|||", 2)
    while len(parts) < 3:
        parts.append("")
    return str(parts[0]), str(parts[1]), str(parts[2])


def _checkpoint_dir(dataset_root: Path, run_id: str) -> Path:
    safe_run_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(run_id or "").strip())
    safe_run_id = safe_run_id or "default"
    return dataset_root / "revalidation_runs" / safe_run_id


def _dataset_fingerprint(version: str, row_count: int) -> str:
    payload = {"version": str(version or ""), "row_count": int(row_count)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


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


def _collect_unique_direct_ids(df: pd.DataFrame, phase2_test_app: Any) -> set[str]:
    ids: set[str] = set()
    for _, row in df.iterrows():
        youtube_ref = _first_nonempty_text(row.get("youtube_link"), row.get("url_youtube"))
        watch_url = phase2_test_app.base_app.normalize_youtube_watch_url(youtube_ref)
        if not watch_url:
            continue
        video_id = phase2_test_app.base_app.extract_youtube_video_id(watch_url)
        if video_id:
            ids.add(str(video_id))
    return ids


def _check_playability_map(
    video_ids: set[str],
    phase2_test_app: Any,
    workers: int = 12,
    existing: dict[str, bool] | None = None,
    checkpoint_every: int = 0,
    checkpoint_callback: Any | None = None,
) -> dict[str, bool]:
    ids = sorted(video_ids)
    if not ids:
        return {}

    result: dict[str, bool] = dict(existing or {})

    def _check(video_id: str) -> tuple[str, bool]:
        ok = bool(phase2_test_app._is_video_playlist_playable_phase2(video_id, timeout=4))
        return video_id, ok

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        futures = [executor.submit(_check, video_id) for video_id in ids]
        total = len(futures)
        done = 0
        for future in as_completed(futures):
            video_id, ok = future.result()
            result[video_id] = ok
            done += 1
            if checkpoint_callback is not None and checkpoint_every > 0 and (done % checkpoint_every == 0 or done == total):
                checkpoint_callback(result)
            if done % 500 == 0 or done == total:
                print(f"[playability] checked {done}/{total}")
    return result


def revalidate_youtube_links(
    workers: int = 12,
    dry_run: bool = False,
    resume: bool = True,
    run_id: str = "",
) -> dict[str, Any]:
    dataset_root = cfg.dataset_root()
    current_df, current_release = load_prepared_dataset(dataset_root=dataset_root)
    current_meta = release_summary(current_release, dataset_root=dataset_root)
    resolved_run_id = str(run_id or f"revalidate_{current_release.version}")
    checkpoint_dir = _checkpoint_dir(dataset_root, resolved_run_id)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_meta_path = checkpoint_dir / "meta.json"
    playability_path = checkpoint_dir / "playability_map.json"
    row_states_path = checkpoint_dir / "row_states.json"
    resolver_cache_path = checkpoint_dir / "resolver_cache.json"
    checkpoint_meta = {
        "run_id": resolved_run_id,
        "parent_dataset_version": current_release.version,
        "dataset_fingerprint": _dataset_fingerprint(current_release.version, len(current_df)),
    }

    if checkpoint_meta_path.exists():
        existing_meta = _read_json(checkpoint_meta_path)
        if resume and existing_meta == checkpoint_meta:
            print(f"[resume] using checkpoint dir: {checkpoint_dir}")
        else:
            for stale_path in (playability_path, row_states_path, resolver_cache_path):
                if stale_path.exists():
                    stale_path.unlink()
    _write_json(checkpoint_meta_path, checkpoint_meta)

    phase2_test_app = _load_phase2_test_module()
    base_app = phase2_test_app.base_app

    df = current_df.copy()
    unique_ids = _collect_unique_direct_ids(df, phase2_test_app)
    print(f"[playability] unique direct ids: {len(unique_ids)}")
    playability_map: dict[str, bool] = {}
    if resume and playability_path.exists():
        loaded_playability = _read_json(playability_path)
        if isinstance(loaded_playability, dict):
            playability_map = {str(key): bool(value) for key, value in loaded_playability.items()}
            print(f"[resume] loaded playability map: {len(playability_map)} ids")
    missing_direct_ids = unique_ids - set(playability_map.keys())
    if missing_direct_ids:
        playability_map = _check_playability_map(
            missing_direct_ids,
            phase2_test_app,
            workers=workers,
            existing=playability_map,
            checkpoint_every=500,
            checkpoint_callback=lambda payload: _write_json(playability_path, payload),
        )
        _write_json(playability_path, playability_map)
        print(f"[checkpoint] wrote playability map: {playability_path}")
    else:
        print("[resume] direct playability already complete")

    resolver_cache: dict[tuple[str, str, str], tuple[str, str, float]] = {}
    direct_playable_count = 0
    no_direct_id_count = 0
    unplayable_count = 0
    replaced_count = 0
    unresolved_count = 0
    no_change_count = 0
    resolver_error_count = 0

    audit_rows: list[dict[str, Any]] = []
    row_states: list[dict[str, Any]] = []
    resolver_keys_to_fetch: set[tuple[str, str, str]] = set()

    total_rows = len(df)
    if resume and row_states_path.exists():
        loaded_row_states = _read_json(row_states_path)
        if isinstance(loaded_row_states, list):
            row_states = list(loaded_row_states)
            print(f"[resume] loaded row states: {len(row_states)} rows")
    if not row_states:
        for idx, row in df.iterrows():
            if (idx + 1) % 1000 == 0 or (idx + 1) == total_rows:
                print(f"[rows] profiled {idx + 1}/{total_rows}")

            artist = str(row.get("artist", "") or "")
            track = str(row.get("track", "") or "")
            credits = str(row.get("artist_credits", "") or "")
            key = str(row.get("canonical_key", "") or "")

            youtube_ref = _first_nonempty_text(row.get("youtube_link"), row.get("url_youtube"))
            direct_watch = base_app.normalize_youtube_watch_url(youtube_ref)
            direct_id = base_app.extract_youtube_video_id(direct_watch) if direct_watch else None
            direct_ok = bool(direct_id and playability_map.get(str(direct_id), False))
            resolver_key = (
                (artist.strip().lower(), track.strip().lower(), credits.strip()) if (direct_id and not direct_ok) else None
            )

            row_states.append(
                {
                    "idx": idx,
                    "canonical_key": key,
                    "artist": artist,
                    "track": track,
                    "credits": credits,
                    "direct_watch": str(direct_watch or ""),
                    "direct_id": str(direct_id or ""),
                    "direct_ok": bool(direct_ok),
                    "resolver_key": _resolver_key_to_text(resolver_key) if resolver_key is not None else "",
                }
            )
        _write_json(row_states_path, row_states)
        print(f"[checkpoint] wrote row states: {row_states_path}")
    for state in row_states:
        resolver_key_text = str(state.get("resolver_key", "") or "")
        if resolver_key_text:
            resolver_keys_to_fetch.add(_resolver_key_from_text(resolver_key_text))

    resolver_keys = sorted(resolver_keys_to_fetch)
    if resume and resolver_cache_path.exists():
        loaded_resolver_cache = _read_json(resolver_cache_path)
        if isinstance(loaded_resolver_cache, dict):
            resolver_cache = {
                _resolver_key_from_text(str(key)): (
                    str(value[0] if len(value) > 0 else ""),
                    str(value[1] if len(value) > 1 else ""),
                    float(value[2] if len(value) > 2 else 0.0),
                )
                for key, value in loaded_resolver_cache.items()
                if isinstance(value, list)
            }
            print(f"[resume] loaded resolver cache: {len(resolver_cache)} keys")
    if resolver_keys:
        pending_resolver_keys = [resolver_key for resolver_key in resolver_keys if resolver_key not in resolver_cache]
        print(f"[resolver] resolving {len(pending_resolver_keys)}/{len(resolver_keys)} unique keys")

        def _resolve_key(resolver_key: tuple[str, str, str]) -> tuple[tuple[str, str, str], tuple[str, str, float], bool]:
            artist_key, track_key, credits_key = resolver_key
            try:
                watch, reason, score = phase2_test_app.resolve_live_youtube_watch_url_phase2(
                    artist=artist_key,
                    track=track_key,
                    artist_credits_json=credits_key,
                    timeout=8,
                )
                return resolver_key, (str(watch or ""), str(reason or ""), float(score or 0.0)), False
            except Exception as exc:
                return resolver_key, ("", f"resolver_exception: {exc}", 0.0), True

        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
            futures = [executor.submit(_resolve_key, resolver_key) for resolver_key in pending_resolver_keys]
            total = len(futures)
            done = 0
            for future in as_completed(futures):
                resolver_key, payload, is_error = future.result()
                resolver_cache[resolver_key] = payload
                if is_error:
                    resolver_error_count += 1
                done += 1
                if done % 100 == 0 or done == total:
                    _write_json(
                        resolver_cache_path,
                        {_resolver_key_to_text(key): [value[0], value[1], value[2]] for key, value in resolver_cache.items()},
                    )
                    print(f"[checkpoint] wrote resolver cache: {len(resolver_cache)} keys")
                if done % 500 == 0 or done == total:
                    print(f"[resolver] resolved {done}/{total}")
        if not pending_resolver_keys:
            print("[resume] resolver stage already complete")

    resolved_candidate_ids: set[str] = set()
    for watch, _reason, _score in resolver_cache.values():
        watch_text = str(watch or "").strip()
        if not watch_text:
            continue
        new_id = base_app.extract_youtube_video_id(watch_text)
        if new_id:
            resolved_candidate_ids.add(str(new_id))
    unresolved_new_ids = resolved_candidate_ids - set(playability_map.keys())
    if unresolved_new_ids:
        print(f"[playability] checking {len(unresolved_new_ids)} resolved ids")
        playability_map = _check_playability_map(
            unresolved_new_ids,
            phase2_test_app,
            workers=workers,
            existing=playability_map,
            checkpoint_every=200,
            checkpoint_callback=lambda payload: _write_json(playability_path, payload),
        )
        _write_json(playability_path, playability_map)

    for pos, state in enumerate(row_states, start=1):
        if pos % 1000 == 0 or pos == total_rows:
            print(f"[rows] processed {pos}/{total_rows}")

        idx = int(state["idx"])
        artist = str(state["artist"])
        track = str(state["track"])
        key = str(state["canonical_key"])
        direct_watch = str(state["direct_watch"])
        direct_id = str(state["direct_id"])
        direct_ok = bool(state["direct_ok"])
        resolver_key_text = str(state.get("resolver_key", "") or "")
        resolver_key = _resolver_key_from_text(resolver_key_text) if resolver_key_text else None

        if not direct_id:
            no_direct_id_count += 1
            audit_rows.append(
                {
                    "canonical_key": key,
                    "artist": artist,
                    "track": track,
                    "old_watch_url": direct_watch,
                    "old_video_id": "",
                    "new_watch_url": "",
                    "new_video_id": "",
                    "status": "no_direct_id",
                    "resolver_reason": "",
                }
            )
            continue

        if direct_ok:
            direct_playable_count += 1
            audit_rows.append(
                {
                    "canonical_key": key,
                    "artist": artist,
                    "track": track,
                    "old_watch_url": direct_watch,
                    "old_video_id": direct_id,
                    "new_watch_url": direct_watch,
                    "new_video_id": direct_id,
                    "status": "kept_direct_playable",
                    "resolver_reason": "",
                }
            )
            continue

        unplayable_count += 1
        new_watch, resolver_reason, _resolver_score = resolver_cache.get(resolver_key, ("", "resolver_not_run", 0.0))
        new_watch = str(new_watch or "").strip()
        new_id = base_app.extract_youtube_video_id(new_watch) if new_watch else None

        if not new_id:
            unresolved_count += 1
            audit_rows.append(
                {
                    "canonical_key": key,
                    "artist": artist,
                    "track": track,
                    "old_watch_url": direct_watch,
                    "old_video_id": direct_id,
                    "new_watch_url": "",
                    "new_video_id": "",
                    "status": "unplayable_no_resolver_match",
                    "resolver_reason": str(resolver_reason or ""),
                }
            )
            continue

        new_ok = bool(playability_map.get(str(new_id), False))
        if not new_ok:
            unresolved_count += 1
            audit_rows.append(
                {
                    "canonical_key": key,
                    "artist": artist,
                    "track": track,
                    "old_watch_url": direct_watch,
                    "old_video_id": direct_id,
                    "new_watch_url": str(new_watch or ""),
                    "new_video_id": str(new_id),
                    "status": "unplayable_resolver_returned_unplayable",
                    "resolver_reason": str(resolver_reason or ""),
                }
            )
            continue

        if str(new_id) == str(direct_id):
            no_change_count += 1
            audit_rows.append(
                {
                    "canonical_key": key,
                    "artist": artist,
                    "track": track,
                    "old_watch_url": direct_watch,
                    "old_video_id": direct_id,
                    "new_watch_url": str(new_watch or ""),
                    "new_video_id": str(new_id),
                    "status": "unplayable_resolved_same_id",
                    "resolver_reason": str(resolver_reason or ""),
                }
            )
            continue

        replaced_count += 1
        df.at[idx, "youtube_link"] = str(new_watch)
        if "url_youtube" in df.columns:
            df.at[idx, "url_youtube"] = str(new_watch)
        if "youtube_video_id" in df.columns:
            df.at[idx, "youtube_video_id"] = str(new_id)
        audit_rows.append(
            {
                "canonical_key": key,
                "artist": artist,
                "track": track,
                "old_watch_url": direct_watch,
                "old_video_id": direct_id,
                "new_watch_url": str(new_watch or ""),
                "new_video_id": str(new_id),
                "status": "unplayable_replaced_by_resolver",
                "resolver_reason": str(resolver_reason or ""),
            }
        )

    summary = {
        "rows_total": int(len(df)),
        "rows_direct_playable": int(direct_playable_count),
        "rows_no_direct_id": int(no_direct_id_count),
        "rows_direct_unplayable": int(unplayable_count),
        "rows_replaced": int(replaced_count),
        "rows_unresolved": int(unresolved_count),
        "rows_no_change": int(no_change_count),
        "resolver_error_keys": int(resolver_error_count),
        "unique_direct_ids_checked": int(len(unique_ids)),
    }

    if dry_run:
        return {
            "status": "dry_run",
            "summary": summary,
            "run_id": resolved_run_id,
            "checkpoint_dir": str(checkpoint_dir.resolve()),
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
    audit_path = snapshot_dir / "youtube_link_revalidation_report.csv"
    audit_df.to_csv(audit_path, index=False)

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
            "dataset_source": "managed_snapshot_revalidation",
            "parent_dataset_version": current_release.version,
            "parent_metadata_path": str(current_release.metadata_path.resolve()),
            "parent_release_summary": current_meta,
        },
        "revalidation": summary
        | {
            "report_path": str(audit_path.resolve()),
            "mode": "direct_link_playability_then_resolver",
            "resolver_module": "go-live/code/test/application_phase2.py",
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
        "dataset_root": str(dataset_root.resolve()),
        "run_id": resolved_run_id,
        "checkpoint_dir": str(checkpoint_dir.resolve()),
        "pointer_path": str(pointer_path.resolve()),
        "metadata_path": str(metadata_path.resolve()),
        "artifacts": {name: str(path.resolve()) for name, path in artifact_paths.items()},
        "report_path": str(audit_path.resolve()),
        "summary": summary,
    }


if __name__ == "__main__":
    workers = 12
    dry_run = False
    resume = True
    run_id = ""
    for arg in sys.argv[1:]:
        text = str(arg).strip()
        if text == "--dry-run":
            dry_run = True
        elif text == "--no-resume":
            resume = False
        elif text == "--resume":
            resume = True
        elif text.startswith("--workers="):
            try:
                workers = int(text.split("=", 1)[1])
            except Exception:
                workers = 12
        elif text.startswith("--run-id="):
            run_id = text.split("=", 1)[1].strip()
    print(
        json.dumps(
            revalidate_youtube_links(workers=workers, dry_run=dry_run, resume=resume, run_id=run_id),
            ensure_ascii=False,
            indent=2,
        )
    )
