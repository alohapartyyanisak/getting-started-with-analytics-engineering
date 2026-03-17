from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class ArtifactPaths:
    audit_md: Path
    overlap_examples_csv: Path
    collision_examples_csv: Path
    source_registry_csv: Path
    manifest_latest_json: Path
    manifest_versioned_json: Path


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _hash_frame(df: pd.DataFrame) -> str:
    if df.empty:
        return hashlib.sha256(b"").hexdigest()
    sortable_cols = sorted(str(col) for col in df.columns)
    stable = df.copy()
    stable = stable.reindex(columns=sortable_cols)
    stable = stable.fillna("")

    def _norm_value(value: Any) -> Any:
        if isinstance(value, (list, dict, set, tuple)):
            try:
                return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            except Exception:
                return str(value)
        return value

    for col in stable.columns:
        if stable[col].dtype == object:
            stable[col] = stable[col].map(_norm_value)

    stable = stable.sort_values(by=sortable_cols, kind="mergesort").reset_index(drop=True)
    row_hash = pd.util.hash_pandas_object(stable, index=False).values.tobytes()
    return hashlib.sha256(row_hash).hexdigest()


def _schema_hash(df: pd.DataFrame) -> str:
    schema = [{"name": str(col), "dtype": str(dtype)} for col, dtype in zip(df.columns, df.dtypes)]
    encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_compact(df: pd.DataFrame, columns: list[str], limit: int = 20) -> pd.DataFrame:
    available = [col for col in columns if col in df.columns]
    if not available:
        return pd.DataFrame()
    return df[available].head(limit).reset_index(drop=True)


def _render_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_No rows._"
    try:
        return df.to_markdown(index=False)
    except Exception:
        return "```text\n" + df.to_string(index=False) + "\n```"


def _build_paths(plan_dir: Path, snapshot_id: str) -> ArtifactPaths:
    manifests_dir = plan_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    return ArtifactPaths(
        audit_md=plan_dir / "12_overlap_collision_audit_report.md",
        overlap_examples_csv=plan_dir / "12_overlap_examples.csv",
        collision_examples_csv=plan_dir / "12_collision_examples.csv",
        source_registry_csv=plan_dir / "source_registry_metadata.csv",
        manifest_latest_json=manifests_dir / "snapshot_manifest_latest.json",
        manifest_versioned_json=manifests_dir / f"snapshot_manifest_{snapshot_id}.json",
    )


def generate() -> int:
    root = Path(__file__).resolve().parents[3]
    offline_code_dir = root / "recommendation_app" / "offline-v2" / "code"
    plan_dir = root / "recommendation_app" / "go-live" / "plan"

    import sys

    if str(offline_code_dir) not in sys.path:
        sys.path.insert(0, str(offline_code_dir))

    import data_upgrade_v2  # type: ignore
    import recommender_v2_adapter  # type: ignore

    timestamp = datetime.now(timezone.utc)
    snapshot_id = timestamp.strftime("ds_%Y%m%d_%H%M%S_utc")
    paths = _build_paths(plan_dir, snapshot_id)

    baseline_dataset_id = data_upgrade_v2.BASELINE_DATASET_ID
    expansion_dataset_id = data_upgrade_v2.EXPANSION_DATASET_ID

    baseline_path = data_upgrade_v2._resolve_cached_dataset_path(baseline_dataset_id)
    expansion_path = data_upgrade_v2._resolve_cached_dataset_path(expansion_dataset_id)
    baseline_csvs = data_upgrade_v2._resolve_dataset_csv(baseline_path, baseline_dataset_id)
    expansion_csvs = data_upgrade_v2._resolve_dataset_csv(expansion_path, expansion_dataset_id)

    baseline_raw = pd.read_csv(baseline_csvs[0])
    expansion_frames = []
    for csv_path in expansion_csvs:
        tier = "high" if "high" in csv_path.name.lower() else ("low" if "low" in csv_path.name.lower() else "unknown")
        expansion_frames.append(pd.read_csv(csv_path).assign(source_tier=tier, source_file=csv_path.name))
    expansion_raw = pd.concat(expansion_frames, ignore_index=True, sort=False)

    baseline_norm = data_upgrade_v2._normalize_columns(baseline_raw)
    expansion_norm = data_upgrade_v2._normalize_columns(expansion_raw)

    # Lightweight canonical views for overlap/collision audit (vectorized, no row-wise enrichment).
    baseline_frame = pd.DataFrame(
        {
            "artist": data_upgrade_v2._series_or_default(baseline_norm, "artist", "").fillna("").astype(str),
            "track": data_upgrade_v2._series_or_default(baseline_norm, "track", "").fillna("").astype(str),
            "uri": data_upgrade_v2._series_or_default(baseline_norm, "uri", "").fillna("").astype(str),
            "url_spotify": data_upgrade_v2._series_or_default(baseline_norm, "url_spotify", "").fillna("").astype(str),
            "source_tier": "baseline",
        }
    )

    expansion_artist = data_upgrade_v2._series_or_default(expansion_norm, "track_artist", "")
    expansion_track = data_upgrade_v2._series_or_default(expansion_norm, "track_name", "")
    expansion_uri = data_upgrade_v2._series_or_default(expansion_norm, "uri", "")
    expansion_track_id = data_upgrade_v2._series_or_default(expansion_norm, "track_id", "")
    expansion_source_tier = data_upgrade_v2._series_or_default(expansion_norm, "source_tier", "unknown")

    expansion_sid_from_uri = expansion_uri.fillna("").astype(str).map(data_upgrade_v2._extract_spotify_track_id_from_uri)
    expansion_sid = expansion_sid_from_uri.where(
        expansion_sid_from_uri.fillna("").astype(str).str.len() > 0,
        expansion_track_id.fillna("").astype(str),
    )
    expansion_url = expansion_sid.fillna("").astype(str).map(
        lambda sid: f"https://open.spotify.com/track/{sid}" if sid else ""
    )

    expansion_frame = pd.DataFrame(
        {
            "artist": expansion_artist.fillna("").astype(str),
            "track": expansion_track.fillna("").astype(str),
            "uri": expansion_uri.fillna("").astype(str),
            "url_spotify": expansion_url,
            "source_tier": expansion_source_tier.fillna("unknown").astype(str),
        }
    )

    # One merged load path (cached when available).
    merged_frame, _, _ = data_upgrade_v2.load_and_merge_from_kagglehub(
        baseline_dataset_id=baseline_dataset_id,
        expansion_dataset_id=expansion_dataset_id,
    )
    prepared_frame = recommender_v2_adapter.prepare_music_data(merged_frame)

    # Derived strict vs relaxed keys for audit metrics.
    baseline_frame = baseline_frame.copy()
    expansion_frame = expansion_frame.copy()
    baseline_frame["artist_strict"] = baseline_frame["artist"].map(data_upgrade_v2._normalize_artist)
    expansion_frame["artist_strict"] = expansion_frame["artist"].map(data_upgrade_v2._normalize_artist)
    baseline_frame["track_strict"] = baseline_frame["track"].map(data_upgrade_v2._normalize_text)
    expansion_frame["track_strict"] = expansion_frame["track"].map(data_upgrade_v2._normalize_text)
    baseline_frame["track_relaxed"] = baseline_frame["track"].map(data_upgrade_v2._normalize_track)
    expansion_frame["track_relaxed"] = expansion_frame["track"].map(data_upgrade_v2._normalize_track)
    baseline_frame["strict_key"] = baseline_frame["artist_strict"] + "|||" + baseline_frame["track_strict"]
    expansion_frame["strict_key"] = expansion_frame["artist_strict"] + "|||" + expansion_frame["track_strict"]
    baseline_frame["relaxed_key"] = baseline_frame["artist_strict"] + "|||" + baseline_frame["track_relaxed"]
    expansion_frame["relaxed_key"] = expansion_frame["artist_strict"] + "|||" + expansion_frame["track_relaxed"]

    # Overlap metrics
    base_strict = set(baseline_frame["strict_key"].astype(str))
    exp_strict = set(expansion_frame["strict_key"].astype(str))
    strict_overlap = base_strict & exp_strict

    base_relaxed = set(baseline_frame["relaxed_key"].astype(str))
    exp_relaxed = set(expansion_frame["relaxed_key"].astype(str))
    relaxed_overlap = base_relaxed & exp_relaxed

    # Formatting-difference examples where relaxed key overlaps but strict key differs
    base_relaxed_map = baseline_frame[
        ["artist", "track", "artist_strict", "track_strict", "track_relaxed", "relaxed_key"]
    ].drop_duplicates(subset=["relaxed_key"], keep="first")
    exp_relaxed_map = expansion_frame[
        ["artist", "track", "artist_strict", "track_strict", "track_relaxed", "source_tier", "relaxed_key"]
    ].drop_duplicates(subset=["relaxed_key"], keep="first")
    formatting_examples = exp_relaxed_map.merge(
        base_relaxed_map,
        on="relaxed_key",
        how="inner",
        suffixes=("_exp", "_base"),
    )
    formatting_examples = formatting_examples.loc[
        (formatting_examples["track_strict_exp"] != formatting_examples["track_strict_base"])
        | (formatting_examples["track_exp"] != formatting_examples["track_base"])
    ].copy()
    formatting_examples = formatting_examples.sort_values(["artist_exp", "track_exp"]).reset_index(drop=True)

    # Collision metrics: same relaxed track with multiple artists
    base_track_artist_counts = baseline_frame.groupby("track_relaxed", as_index=False)["artist_strict"].nunique().rename(
        columns={"artist_strict": "artist_count"}
    )
    exp_track_artist_counts = expansion_frame.groupby("track_relaxed", as_index=False)["artist_strict"].nunique().rename(
        columns={"artist_strict": "artist_count"}
    )
    base_track_artist_counts = base_track_artist_counts.loc[base_track_artist_counts["track_relaxed"].astype(str).str.strip() != ""]
    exp_track_artist_counts = exp_track_artist_counts.loc[exp_track_artist_counts["track_relaxed"].astype(str).str.strip() != ""]
    base_collisions = base_track_artist_counts.loc[base_track_artist_counts["artist_count"] > 1].copy()
    exp_collisions = exp_track_artist_counts.loc[exp_track_artist_counts["artist_count"] > 1].copy()

    collision_examples = (
        expansion_frame.merge(exp_collisions[["track_relaxed"]], on="track_relaxed", how="inner")
        [["source_tier", "artist", "track", "track_relaxed", "url_spotify", "uri"]]
        .loc[lambda d: d["artist"].astype(str).str.strip() != ""]
        .drop_duplicates(subset=["artist", "track", "track_relaxed"], keep="first")
        .sort_values(["track_relaxed", "artist"])
        .reset_index(drop=True)
    )

    # overlap example export
    overlap_examples = (
        expansion_frame.loc[lambda d: d["strict_key"].isin(strict_overlap)]
        [["source_tier", "artist", "track", "url_spotify", "uri", "strict_key"]]
        .drop_duplicates(subset=["strict_key"], keep="first")
        .sort_values(["artist", "track"])
        .reset_index(drop=True)
    )

    overlap_examples.to_csv(paths.overlap_examples_csv, index=False)
    collision_examples.to_csv(paths.collision_examples_csv, index=False)

    # Source registry metadata (T-005)
    source_rows: list[dict[str, Any]] = []
    for dataset_name, dataset_id, csv_path, frame in [
        ("baseline", baseline_dataset_id, baseline_csvs[0], baseline_raw),
        ("expansion_high", expansion_dataset_id, next((p for p in expansion_csvs if "high" in p.name.lower()), expansion_csvs[0]), expansion_raw.loc[expansion_raw.get("source_tier", "").astype(str) == "high"]),
        ("expansion_low", expansion_dataset_id, next((p for p in expansion_csvs if "low" in p.name.lower()), expansion_csvs[-1]), expansion_raw.loc[expansion_raw.get("source_tier", "").astype(str) == "low"]),
    ]:
        stat = csv_path.stat()
        row_count = int(len(frame))
        source_rows.append(
            {
                "source_name": dataset_name,
                "dataset_id": dataset_id,
                "source_path": str(csv_path),
                "version_dir": str(csv_path.parent),
                "row_count": row_count,
                "column_count": int(frame.shape[1]) if not frame.empty else int(len(frame.columns)),
                "sha256_file": _sha256_file(csv_path),
                "last_modified_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "freshness_basis": "file_modified_time",
                "quality_tier": "baseline" if dataset_name == "baseline" else dataset_name.replace("expansion_", ""),
                "status": "active",
                "notes": "auto-generated",
            }
        )

    source_rows.append(
        {
            "source_name": "merged_offline_v2",
            "dataset_id": f"{baseline_dataset_id} + {expansion_dataset_id}",
            "source_path": "runtime_merge",
            "version_dir": "runtime",
            "row_count": int(len(merged_frame)),
            "column_count": int(merged_frame.shape[1]),
            "sha256_file": _hash_frame(merged_frame),
            "last_modified_utc": timestamp.isoformat(),
            "freshness_basis": "generated_at_runtime",
            "quality_tier": "merged",
            "status": "active",
            "notes": "hash is row-content hash, not file hash",
        }
    )
    source_registry = pd.DataFrame(source_rows)
    source_registry.to_csv(paths.source_registry_csv, index=False)

    # Snapshot manifest (T-004)
    manifest = {
        "snapshot_id": snapshot_id,
        "created_at_utc": timestamp.isoformat(),
        "datasets": {
            "baseline_dataset_id": baseline_dataset_id,
            "expansion_dataset_id": expansion_dataset_id,
        },
        "source_files": [
            {
                "path": str(p),
                "sha256": _sha256_file(p),
                "size_bytes": p.stat().st_size,
                "mtime_utc": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
            }
            for p in [baseline_csvs[0], *expansion_csvs]
        ],
        "row_counts": {
            "baseline_raw": int(len(baseline_raw)),
            "expansion_raw": int(len(expansion_raw)),
            "baseline_canonical": int(len(baseline_frame)),
            "expansion_canonical": int(len(expansion_frame)),
            "merged": int(len(merged_frame)),
            "prepared": int(len(prepared_frame)),
        },
        "unique_counts": {
            "merged_artists": int(merged_frame["artist"].nunique()),
            "merged_tracks": int(merged_frame["track"].nunique()),
            "prepared_display_names": int(prepared_frame["display_name"].nunique()),
        },
        "hashes": {
            "merged_row_hash_sha256": _hash_frame(merged_frame),
            "prepared_row_hash_sha256": _hash_frame(prepared_frame),
            "merged_schema_hash_sha256": _schema_hash(merged_frame),
            "prepared_schema_hash_sha256": _schema_hash(prepared_frame),
        },
        "overlap_metrics": {
            "baseline_unique_strict_keys": int(len(base_strict)),
            "expansion_unique_strict_keys": int(len(exp_strict)),
            "strict_overlap_keys": int(len(strict_overlap)),
            "strict_overlap_rate_in_expansion": round((len(strict_overlap) / len(exp_strict)) if exp_strict else 0.0, 6),
            "baseline_unique_relaxed_keys": int(len(base_relaxed)),
            "expansion_unique_relaxed_keys": int(len(exp_relaxed)),
            "relaxed_overlap_keys": int(len(relaxed_overlap)),
            "relaxed_overlap_rate_in_expansion": round((len(relaxed_overlap) / len(exp_relaxed)) if exp_relaxed else 0.0, 6),
            "baseline_track_collision_keys": int(len(base_collisions)),
            "expansion_track_collision_keys": int(len(exp_collisions)),
        },
        "artifacts": {
            "audit_report": str(paths.audit_md),
            "source_registry_csv": str(paths.source_registry_csv),
            "overlap_examples_csv": str(paths.overlap_examples_csv),
            "collision_examples_csv": str(paths.collision_examples_csv),
        },
    }
    paths.manifest_versioned_json.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    paths.manifest_latest_json.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    # Audit report markdown (T-003)
    sample_overlap = _safe_compact(overlap_examples, ["source_tier", "artist", "track", "url_spotify"], limit=12)
    sample_collisions = _safe_compact(collision_examples, ["source_tier", "artist", "track", "track_relaxed", "url_spotify"], limit=20)
    sample_formatting = _safe_compact(
        formatting_examples.rename(
            columns={
                "artist_exp": "exp_artist",
                "track_exp": "exp_track",
                "artist_base": "base_artist",
                "track_base": "base_track",
            }
        ),
        ["source_tier", "exp_artist", "exp_track", "base_artist", "base_track", "relaxed_key"],
        limit=12,
    )

    report = []
    report.append("# Overlap and Collision Audit Report (T-003)")
    report.append("")
    report.append(f"Generated at: `{timestamp.isoformat()}`")
    report.append(f"Snapshot ID: `{snapshot_id}`")
    report.append("")
    report.append("## Summary")
    report.append(f"- Baseline rows: `{len(baseline_raw):,}`")
    report.append(f"- Expansion rows: `{len(expansion_raw):,}`")
    report.append(f"- Merged rows: `{len(merged_frame):,}`")
    report.append(f"- Strict overlap: `{len(strict_overlap):,}` / `{len(exp_strict):,}` ({(len(strict_overlap)/len(exp_strict) if exp_strict else 0):.2%})")
    report.append(f"- Relaxed overlap: `{len(relaxed_overlap):,}` / `{len(exp_relaxed):,}` ({(len(relaxed_overlap)/len(exp_relaxed) if exp_relaxed else 0):.2%})")
    report.append(f"- Track-text collision keys (baseline): `{len(base_collisions):,}`")
    report.append(f"- Track-text collision keys (expansion): `{len(exp_collisions):,}`")
    report.append("")
    report.append("## Matching Notes")
    report.append("- Strict key: normalized `artist + raw-normalized track`.")
    report.append("- Relaxed key: normalized `artist + cleaned track` (feat/version cleanup).")
    report.append("- Collisions are identified where one relaxed track maps to multiple normalized artists.")
    report.append("")
    report.append("## Example Strict Overlaps")
    if sample_overlap.empty:
        report.append("_No strict overlap examples found._")
    else:
        report.append(_render_table(sample_overlap))
    report.append("")
    report.append("## Examples With Different Raw Values But Same Relaxed Song")
    if sample_formatting.empty:
        report.append("_No formatting-difference examples found._")
    else:
        report.append(_render_table(sample_formatting))
    report.append("")
    report.append("## Collision Examples (Same Relaxed Track, Different Artists)")
    if sample_collisions.empty:
        report.append("_No collision examples found._")
    else:
        report.append(_render_table(sample_collisions))
    report.append("")
    report.append("## Output Artifacts")
    report.append(f"- Overlap examples CSV: `{paths.overlap_examples_csv}`")
    report.append(f"- Collision examples CSV: `{paths.collision_examples_csv}`")
    report.append(f"- Source registry CSV: `{paths.source_registry_csv}`")
    report.append(f"- Snapshot manifest: `{paths.manifest_versioned_json}`")
    report.append("")
    report.append("## Sign-Off")
    report.append("- Status: `Generated`")
    report.append("- Reviewer: `_pending_`")
    report.append("- Notes: `_pending_`")
    report.append("")

    paths.audit_md.write_text("\n".join(report), encoding="utf-8")

    print(
        json.dumps(
            {
                "status": "ok",
                "snapshot_id": snapshot_id,
                "audit_report": str(paths.audit_md),
                "source_registry_csv": str(paths.source_registry_csv),
                "manifest_latest": str(paths.manifest_latest_json),
                "manifest_versioned": str(paths.manifest_versioned_json),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(generate())
