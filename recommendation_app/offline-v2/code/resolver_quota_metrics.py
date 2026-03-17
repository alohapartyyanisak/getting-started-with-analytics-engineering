from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

import runtime_config

def run_metrics(budget_units: int, max_requests: int, timeout: int) -> int:
    code_dir = Path(__file__).resolve().parent
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))

    import data_upgrade_v2  # type: ignore
    import recommender_v2_adapter  # type: ignore
    import youtube_live_resolver as ylr  # type: ignore

    api_key = runtime_config.youtube_api_key()
    if not api_key:
        print(json.dumps({"error": "YOUTUBE_API_KEY not set in environment"}, ensure_ascii=False))
        return 2

    # Per-run budget/session counters consumed by youtube_live_resolver internals.
    runtime_config.set_youtube_api_run_budget_units(max(0, int(budget_units)))
    runtime_config.set_youtube_api_units_used_session(0)

    raw, _, _ = data_upgrade_v2.load_and_merge_from_kagglehub()
    prepared = recommender_v2_adapter.prepare_music_data(raw)

    yt = prepared.get("youtube_link", pd.Series([""] * len(prepared), index=prepared.index)).fillna("").astype(str)
    missing = prepared.loc[~yt.str.contains(r"(?:watch\?v=|youtu\.be/)", regex=True)].copy()
    missing = missing.drop_duplicates(subset=["artist", "track"], keep="first")
    missing["popularity_proxy"] = (
        pd.to_numeric(missing.get("views"), errors="coerce").fillna(0)
        + pd.to_numeric(missing.get("stream"), errors="coerce").fillna(0)
    )
    missing = missing.sort_values(["popularity_proxy"], ascending=False).reset_index(drop=True)

    baseline_unresolved = int(len(missing))

    api_counts = {"search": 0, "videos": 0, "channels": 0, "other": 0}
    orig_http_get_json = ylr._http_get_json

    def wrapped_http_get_json(url: str, timeout: int = 8):
        if "youtube/v3/search" in url:
            api_counts["search"] += 1
        elif "youtube/v3/videos" in url:
            api_counts["videos"] += 1
        elif "youtube/v3/channels" in url:
            api_counts["channels"] += 1
        else:
            api_counts["other"] += 1
        return orig_http_get_json(url, timeout=timeout)

    ylr._http_get_json = wrapped_http_get_json

    def est_units() -> int:
        return api_counts["search"] * 100 + api_counts["videos"] + api_counts["channels"]

    results: list[dict[str, object]] = []
    started = time.time()
    quota_blocked = False

    for _, row in missing.iterrows():
        if len(results) >= max_requests:
            break
        if est_units() >= budget_units:
            break
        if ylr._quota_lock_active():
            quota_blocked = True
            break

        artist = str(row.get("artist", "")).strip()
        track = str(row.get("track", "")).strip()
        credits = str(row.get("artist_credits", ""))

        try:
            resolved = ylr.resolve_best_youtube_live(
                artist,
                track,
                credits,
                max_results=10,
                max_query_variants=2,
                timeout=timeout,
            )
            link = str(resolved.get("watch_url", ""))
            conf = str(resolved.get("confidence", "Low"))
            reason = str(resolved.get("reason", ""))
            status = "resolved" if link else "unresolved"
            if "quota exceeded" in reason.lower():
                quota_blocked = True
            results.append(
                {
                    "artist": artist,
                    "track": track,
                    "status": status,
                    "confidence": conf,
                    "score": float(resolved.get("score", 0.0) or 0.0),
                    "has_link": bool(link),
                    "cache_hit": bool(resolved.get("cache_hit", False)),
                    "api_source": str(resolved.get("api_source", "")),
                    "reason": reason,
                }
            )
        except Exception as exc:  # pragma: no cover - operational script
            results.append(
                {
                    "artist": artist,
                    "track": track,
                    "status": "error",
                    "confidence": "Low",
                    "score": 0.0,
                    "has_link": False,
                    "cache_hit": False,
                    "api_source": "error",
                    "reason": f"exception: {exc}",
                }
            )

    elapsed = time.time() - started
    res_df = pd.DataFrame(results)

    resolver_requests_total = int(len(res_df))
    cache_hit_total = int(res_df["cache_hit"].sum()) if not res_df.empty else 0
    cache_miss_total = max(0, resolver_requests_total - cache_hit_total)
    resolved_total = int(res_df["has_link"].sum()) if not res_df.empty else 0
    errors_total = int((res_df["status"] == "error").sum()) if not res_df.empty else 0
    high_conf_total = int((res_df["confidence"] == "High").sum()) if not res_df.empty else 0
    medium_conf_total = int((res_df["confidence"] == "Medium").sum()) if not res_df.empty else 0
    low_conf_total = int((res_df["confidence"] == "Low").sum()) if not res_df.empty else 0

    resolved_high_confidence_rate = (high_conf_total / resolved_total) if resolved_total else 0.0
    resolved_rate_batch = (resolved_total / resolver_requests_total) if resolver_requests_total else 0.0
    unresolved_remaining_estimate = baseline_unresolved - resolved_total

    metrics = {
        "baseline_no_direct_urls": baseline_unresolved,
        "resolver_requests_total": resolver_requests_total,
        "cache_hit_total": cache_hit_total,
        "cache_miss_total": cache_miss_total,
        "resolved_total": resolved_total,
        "errors_total": errors_total,
        "confidence_counts": {
            "High": high_conf_total,
            "Medium": medium_conf_total,
            "Low": low_conf_total,
        },
        "resolved_rate_batch": round(resolved_rate_batch, 4),
        "resolved_high_confidence_rate": round(resolved_high_confidence_rate, 4),
        "api_calls_search_list": api_counts["search"],
        "api_calls_videos_list": api_counts["videos"],
        "api_calls_channels_list": api_counts["channels"],
        "api_calls_other": api_counts["other"],
        "api_calls_total": api_counts["search"] + api_counts["videos"] + api_counts["channels"] + api_counts["other"],
        "quota_units_used_estimate": est_units(),
        "quota_units_used_session_env": runtime_config.youtube_api_units_used_session(),
        "quota_budget_units": budget_units,
        "quota_blocked": quota_blocked,
        "run_seconds": round(elapsed, 2),
        "unresolved_remaining_count_estimate": unresolved_remaining_estimate,
    }

    print("METRICS_JSON_START")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print("METRICS_JSON_END")

    if not res_df.empty:
        sample_cols = ["artist", "track", "status", "confidence", "score", "cache_hit", "api_source", "reason"]
        preview = res_df[sample_cols].head(10)
        print("SAMPLE_RESULTS_START")
        print(preview.to_string(index=False))
        print("SAMPLE_RESULTS_END")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live YouTube resolver calls and print monitoring metrics.")
    parser.add_argument("--budget-units", type=int, default=8500, help="Estimated daily units budget for this run.")
    parser.add_argument("--max-requests", type=int, default=150, help="Maximum tracks to resolve this run.")
    parser.add_argument("--timeout", type=int, default=10, help="HTTP timeout seconds for resolver calls.")
    args = parser.parse_args()
    return run_metrics(args.budget_units, args.max_requests, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
