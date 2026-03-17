from __future__ import annotations

import random
import sys
from pathlib import Path

import pandas as pd


def _sample_candidates(prepared: pd.DataFrame, limit: int, seed: int | None) -> tuple[pd.DataFrame, int]:
    no_direct = prepared.loc[
        ~prepared["youtube_link"].fillna("").astype(str).str.contains(r"watch\?v=", regex=True)
    ].copy()
    no_direct["popularity_proxy"] = (
        pd.to_numeric(no_direct.get("views"), errors="coerce").fillna(0)
        + pd.to_numeric(no_direct.get("stream"), errors="coerce").fillna(0)
    )
    no_direct = no_direct.sort_values(["popularity_proxy"], ascending=False)
    no_direct = no_direct.drop_duplicates(subset=["artist", "track"], keep="first")

    if no_direct.empty:
        return no_direct, 0

    resolved_seed = int(seed) if seed is not None else random.SystemRandom().randint(1, 2_147_483_647)
    sample_count = min(max(1, int(limit)), len(no_direct))
    sample_pool = no_direct.head(min(len(no_direct), 500)).copy()
    sample_pool["sampling_weight"] = sample_pool["popularity_proxy"].clip(lower=0) + 1.0
    sampled = sample_pool.sample(
        n=sample_count,
        replace=False,
        weights="sampling_weight",
        random_state=resolved_seed,
    )
    return sampled, resolved_seed


def _run_quality_audit(prepared: pd.DataFrame, resolve_best_youtube_live, limit: int, seed: int | None) -> int:
    sampled, resolved_seed = _sample_candidates(prepared, limit=limit, seed=seed)
    if sampled.empty:
        print("No rows need live YouTube resolving.")
        return 0

    print(f"audit_seed={resolved_seed}")
    rows = []
    for _, row in sampled.iterrows():
        artist = str(row.get("artist", "")).strip()
        track = str(row.get("track", "")).strip()
        credits = str(row.get("artist_credits", ""))
        resolved = resolve_best_youtube_live(artist, track, credits)
        candidates = resolved.get("candidates", []) or []
        best = candidates[0] if candidates else {}

        anchor_coverage = float(best.get("anchor_coverage", 0.0) or 0.0)
        artist_identity = float(best.get("artist_identity", 0.0) or 0.0)
        official_signal = float(best.get("official_signal", 0.0) or 0.0)
        low_trust = bool(best.get("low_trust", False))
        resolved_link = str(resolved.get("watch_url", ""))
        confidence = str(resolved.get("confidence", "Low"))

        # Generic quality rules, no exact-track expectations.
        risky = (
            (not resolved_link)
            or low_trust
            or anchor_coverage < 0.50
            or artist_identity < 0.75
            or (confidence == "Low" and official_signal <= 0.0)
        )

        rows.append(
            {
                "track-artist": f"{artist} - {track}",
                "resolved_link": resolved_link,
                "confidence": confidence,
                "score": float(resolved.get("score", 0.0) or 0.0),
                "anchor_coverage": round(anchor_coverage, 3),
                "artist_identity": round(artist_identity, 3),
                "official_signal": round(official_signal, 3),
                "low_trust": low_trust,
                "risky": risky,
                "reason": str(resolved.get("reason", "")),
            }
        )

    output = pd.DataFrame(rows)
    pd.set_option("display.max_colwidth", 140)
    resolved_rate = float(output["resolved_link"].astype(str).str.len().gt(0).mean())
    high_conf_rate = float((output["confidence"] == "High").mean())
    risky_rate = float(output["risky"].mean())
    print(
        "Audit summary:",
        f"rows={len(output)}",
        f"resolved_rate={resolved_rate:.2%}",
        f"high_confidence_rate={high_conf_rate:.2%}",
        f"risky_rate={risky_rate:.2%}",
    )
    risky_df = output.loc[output["risky"]].copy()
    if risky_df.empty:
        print("No risky rows detected.")
        return 0

    print("Risky rows:")
    print(
        risky_df[
            [
                "track-artist",
                "resolved_link",
                "confidence",
                "score",
                "anchor_coverage",
                "artist_identity",
                "official_signal",
                "low_trust",
                "reason",
            ]
        ].to_string(index=False)
    )
    return 1


def main(limit: int = 10, seed: int | None = None, run_audit: bool = False) -> int:
    code_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(code_dir))

    import data_upgrade_v2  # type: ignore
    import recommender_v2_adapter  # type: ignore
    from youtube_live_resolver import resolve_best_youtube_live  # type: ignore

    raw, _, _ = data_upgrade_v2.load_and_merge_from_kagglehub()
    prepared = recommender_v2_adapter.prepare_music_data(raw)

    if run_audit:
        return _run_quality_audit(prepared, resolve_best_youtube_live, limit=limit, seed=seed)

    sampled, resolved_seed = _sample_candidates(prepared, limit=limit, seed=seed)
    if sampled.empty:
        print("No rows need live YouTube resolving.")
        return 0

    print(f"sample_seed={resolved_seed}")

    rows = []
    for _, row in sampled.iterrows():
        artist = str(row.get("artist", "")).strip()
        track = str(row.get("track", "")).strip()
        credits = str(row.get("artist_credits", ""))
        resolved = resolve_best_youtube_live(artist, track, credits)
        rows.append(
            {
                "track-artist": f"{artist} - {track}",
                "resolved_link": str(resolved.get("watch_url", "")),
                "reason": str(resolved.get("reason", "")),
                "score": float(resolved.get("score", 0.0) or 0.0),
            }
        )

    output = pd.DataFrame(rows)
    pd.set_option("display.max_colwidth", 140)
    print(output.to_string(index=False))
    return 0


if __name__ == "__main__":
    arg_limit = 10
    arg_seed: int | None = None
    arg_run_audit = False
    if len(sys.argv) > 1:
        try:
            arg_limit = int(sys.argv[1])
        except ValueError:
            if sys.argv[1].strip().lower() in {"--audit", "audit", "--check", "check"}:
                arg_run_audit = True
            else:
                arg_limit = 10
    if len(sys.argv) > 2:
        try:
            arg_seed = int(sys.argv[2])
        except ValueError:
            if sys.argv[2].strip().lower() in {"--audit", "audit", "--check", "check"}:
                arg_run_audit = True
            else:
                arg_seed = None
    if len(sys.argv) > 3 and sys.argv[3].strip().lower() in {"--audit", "audit", "--check", "check"}:
        arg_run_audit = True

    exit_code = main(arg_limit, arg_seed, run_audit=arg_run_audit)
    raise SystemExit(exit_code)
