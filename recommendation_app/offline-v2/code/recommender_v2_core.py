from __future__ import annotations

import re
from typing import Dict, List
from urllib.parse import parse_qs, quote_plus, urlparse

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = {
    "artist",
    "track",
    "danceability",
    "energy",
    "acousticness",
    "instrumentalness",
    "liveness",
    "valence",
    "tempo",
    "views",
    "likes",
    "comments",
    "stream",
}

FEATURE_COLUMNS = [
    "danceability",
    "energy",
    "acousticness",
    "instrumentalness",
    "liveness",
    "valence",
    "speechiness",
    "tempo_scaled",
]

MOOD_ADJUSTMENTS: Dict[str, Dict[str, float]] = {
    "Balanced": {},
    "High Energy": {"energy": 0.30, "danceability": 0.15, "acousticness": -0.20, "tempo_scaled": 0.15},
    "Chill": {"energy": -0.20, "acousticness": 0.20, "tempo_scaled": -0.20, "instrumentalness": 0.08},
    "Dark": {"valence": -0.35, "energy": 0.05, "acousticness": 0.08},
    "Uplifting": {"valence": 0.35, "energy": 0.15, "danceability": 0.10},
}


def _clean_text(value: object) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(value).lower()).strip()


def _split_artist_tokens(text: object) -> list[str]:
    raw = str(text or "")
    if not raw:
        return []
    parts = re.split(r",|&| and ", raw, flags=re.IGNORECASE)
    return [part.strip() for part in parts if part and str(part).strip()]


def _extract_featured_artists(track: object) -> list[str]:
    track_text = str(track or "")
    if not track_text:
        return []

    match = re.search(r"\((?:feat\.?|ft\.?)\s*([^)]+)\)", track_text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"(?:feat\.?|ft\.?)\s+(.+)$", track_text, flags=re.IGNORECASE)
    if not match:
        return []

    featured_block = match.group(1)
    return _split_artist_tokens(featured_block)


def _strip_feature_from_track(track: object) -> str:
    track_text = str(track or "")
    if not track_text:
        return ""
    stripped = re.sub(r"\s*\((?:feat\.?|ft\.?).*?\)", "", track_text, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*(?:feat\.?|ft\.?)\s+.+$", "", stripped, flags=re.IGNORECASE)
    return stripped.strip()


def _extract_spotify_track_id(uri: object, url_spotify: object) -> str | None:
    uri_text = str(uri or "")
    uri_match = re.search(r"spotify:track:([A-Za-z0-9]+)", uri_text)
    if uri_match:
        return uri_match.group(1)

    spotify_url = str(url_spotify or "")
    url_match = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]+)", spotify_url)
    if url_match:
        return url_match.group(1)
    return None


def _extract_youtube_video_id(url: object) -> str | None:
    url_text = str(url or "").strip()
    if not url_text:
        return None

    def _clean_video_id(value: object) -> str | None:
        candidate = str(value or "").strip()
        candidate = candidate.split("?", 1)[0].split("&", 1)[0].split("#", 1)[0]
        return candidate if re.fullmatch(r"[A-Za-z0-9_-]{6,15}", candidate) else None

    parsed = urlparse(url_text)
    if not parsed.netloc and parsed.path:
        parsed = urlparse(f"https://{url_text}")

    host = parsed.netloc.lower().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    path_parts = [part for part in parsed.path.split("/") if part]

    if host == "youtu.be" and path_parts:
        video_id = _clean_video_id(path_parts[0])
        if video_id:
            return video_id
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}:
        if parsed.path == "/watch":
            video_id = _clean_video_id(parse_qs(parsed.query).get("v", [None])[0])
            if video_id:
                return video_id
        if path_parts and path_parts[0] in {"shorts", "embed", "live", "v"} and len(path_parts) > 1:
            video_id = _clean_video_id(path_parts[1])
            if video_id:
                return video_id

    for pattern in (
        r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:embed|shorts|live|v)/)([A-Za-z0-9_-]{6,15})",
        r"youtube\.com/watch/([A-Za-z0-9_-]{6,15})",
        r"[?&]v=([A-Za-z0-9_-]{6,15})",
    ):
        regex_match = re.search(pattern, url_text)
        if regex_match:
            video_id = _clean_video_id(regex_match.group(1))
            if video_id:
                return video_id

    if "/" not in url_text and "?" not in url_text and "&" not in url_text:
        return _clean_video_id(url_text)
    return None


def _youtube_match_score(title: object, artist: object, track: object) -> float:
    title_text = _clean_text(title)
    if not title_text:
        return 0.0

    score = 0.0
    artist_text = _clean_text(artist)
    track_text = _clean_text(track)
    core_track_text = _clean_text(_strip_feature_from_track(track))
    featured_artists = [_clean_text(name) for name in _extract_featured_artists(track)]

    if artist_text and artist_text in title_text:
        score += 1.0
    if core_track_text and core_track_text in title_text:
        score += 1.0
    if track_text and track_text in title_text:
        score += 0.4
    featured_hits = sum(1 for name in featured_artists if name and name in title_text)
    score += min(1.0, featured_hits * 0.35)
    return score


def _build_spotify_link(row: pd.Series) -> str:
    track_id = _extract_spotify_track_id(row.get("uri"), row.get("url_spotify"))
    if track_id:
        return f"https://open.spotify.com/track/{track_id}"

    query = quote_plus(f"{row.get('artist', '')} {row.get('track', '')}".strip())
    return f"https://open.spotify.com/search/{query}" if query else "https://open.spotify.com"


def _build_youtube_link(row: pd.Series) -> str:
    video_id = _extract_youtube_video_id(row.get("url_youtube"))
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"

    core_track = _strip_feature_from_track(row.get("track"))
    query = quote_plus(f"{row.get('artist', '')} {core_track or row.get('track', '')} official".strip())
    return f"https://www.youtube.com/results?search_query={query}" if query else "https://www.youtube.com"


def _link_quality_score(row: pd.Series) -> float:
    spotify_score = 1.0 if _extract_spotify_track_id(row.get("uri"), row.get("url_spotify")) else 0.0
    youtube_video_score = 1.0 if _extract_youtube_video_id(row.get("url_youtube")) else 0.0
    youtube_title_score = _youtube_match_score(row.get("title"), row.get("artist"), row.get("track"))
    return spotify_score + youtube_video_score + youtube_title_score


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()
    normalized.columns = [str(col).strip().lower() for col in normalized.columns]
    return normalized


def _normalize_series(series: pd.Series) -> pd.Series:
    series = series.fillna(0.0).astype(float)
    min_value = float(series.min())
    max_value = float(series.max())
    if np.isclose(min_value, max_value):
        return pd.Series(np.zeros(len(series)), index=series.index, dtype=float)
    return (series - min_value) / (max_value - min_value)


def _drop_duplicate_rows_by_nonempty_key(frame: pd.DataFrame, key_col: str) -> pd.DataFrame:
    if key_col not in frame.columns:
        return frame
    out = frame.copy()
    key_series = out[key_col].fillna("").astype(str).str.strip()
    nonempty = key_series != ""
    if not nonempty.any():
        return out
    duplicate_nonempty = key_series[nonempty].duplicated(keep="first")
    drop_index = duplicate_nonempty[duplicate_nonempty].index
    if len(drop_index) == 0:
        return out
    return out.drop(index=drop_index)


def _dedupe_ranked_candidates_by_links(frame: pd.DataFrame) -> pd.DataFrame:
    """
    Remove duplicate candidate rows that map to the same Spotify track or YouTube video.
    Priority is preserved by expected pre-sorting on recommendation_score/momentum.
    """
    out = frame.copy()

    if "spotify_track_id" not in out.columns:
        spotify_source = out.get("spotify_link", out.get("url_spotify", ""))
        out["spotify_track_id"] = (
            spotify_source.astype(str).apply(lambda value: _extract_spotify_track_id("", value))
            if isinstance(spotify_source, pd.Series)
            else ""
        )
    if "youtube_video_id" not in out.columns:
        youtube_source = out.get("youtube_link", out.get("url_youtube", ""))
        out["youtube_video_id"] = (
            youtube_source.astype(str).apply(_extract_youtube_video_id)
            if isinstance(youtube_source, pd.Series)
            else ""
        )

    out = _drop_duplicate_rows_by_nonempty_key(out, "spotify_track_id")
    out = _drop_duplicate_rows_by_nonempty_key(out, "youtube_video_id")
    return out


def _ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    aliased = df.rename(columns={"streams": "stream"})
    missing = REQUIRED_COLUMNS - set(aliased.columns)
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise ValueError(f"Missing required columns: {missing_list}")
    return aliased


def prepare_music_data(df: pd.DataFrame) -> pd.DataFrame:
    data = _ensure_columns(_normalize_columns(df)).copy()

    for optional_col in ("url_spotify", "url_youtube", "uri", "title", "official_video"):
        if optional_col not in data.columns:
            data[optional_col] = ""

    for col in REQUIRED_COLUMNS - {"artist", "track"}:
        data[col] = pd.to_numeric(data[col], errors="coerce")

    if "speechiness" not in data.columns:
        data["speechiness"] = 0.0

    data["artist"] = data["artist"].astype(str).str.strip()
    data["track"] = data["track"].astype(str).str.strip()
    data = data[(data["artist"] != "") & (data["track"] != "")]

    numeric_defaults = {
        "danceability": data["danceability"].median(),
        "energy": data["energy"].median(),
        "acousticness": data["acousticness"].median(),
        "instrumentalness": data["instrumentalness"].median(),
        "liveness": data["liveness"].median(),
        "valence": data["valence"].median(),
        "speechiness": data["speechiness"].median(),
        "tempo": data["tempo"].median(),
        "views": 0,
        "likes": 0,
        "comments": 0,
        "stream": 0,
    }

    for col, default in numeric_defaults.items():
        data[col] = data[col].fillna(0 if pd.isna(default) else default)

    data["engagement_rate"] = (data["likes"] + data["comments"]) / data["views"].replace(0, np.nan)
    data["stream_to_view_ratio"] = data["stream"] / data["views"].replace(0, np.nan)
    data["engagement_rate"] = data["engagement_rate"].fillna(0.0)
    data["stream_to_view_ratio"] = data["stream_to_view_ratio"].fillna(0.0)

    data["views_norm"] = _normalize_series(np.log1p(data["views"]))
    data["stream_norm"] = _normalize_series(np.log1p(data["stream"]))
    data["engagement_norm"] = _normalize_series(data["engagement_rate"])
    data["ratio_norm"] = _normalize_series(data["stream_to_view_ratio"])
    data["tempo_scaled"] = _normalize_series(data["tempo"])
    data["popularity_norm"] = _normalize_series(np.log1p(data["views"] + data["stream"]))

    data["momentum_score"] = (data["views_norm"] + data["engagement_norm"] + data["ratio_norm"]) / 3.0
    data["link_quality_score"] = data.apply(_link_quality_score, axis=1)
    data["spotify_link"] = data.apply(_build_spotify_link, axis=1)
    data["youtube_link"] = data.apply(_build_youtube_link, axis=1)
    data["display_name"] = data["artist"] + " - " + data["track"]
    data["artist_clean"] = data["artist"].apply(_clean_text)
    data["featured_artists"] = data["track"].apply(_extract_featured_artists)
    data["featured_artist_norms"] = data["featured_artists"].apply(
        lambda artists: "|".join(sorted({_clean_text(name) for name in artists if _clean_text(name)}))
    )
    data["artist_is_featured_on_track"] = data.apply(
        lambda row: row["artist_clean"] in set(row["featured_artist_norms"].split("|")) if row["featured_artist_norms"] else False,
        axis=1,
    )
    data["spotify_track_id"] = data.apply(lambda row: _extract_spotify_track_id(row.get("uri"), row.get("url_spotify")), axis=1)
    data["youtube_video_id"] = data["url_youtube"].apply(_extract_youtube_video_id)
    fallback_song_key = data["artist_clean"] + "::" + data["track"].apply(_clean_text)
    data["canonical_song_key"] = np.where(
        data["spotify_track_id"].fillna("").astype(str).str.len() > 0,
        "sp:" + data["spotify_track_id"].fillna("").astype(str),
        np.where(
            data["youtube_video_id"].fillna("").astype(str).str.len() > 0,
            "yt:" + data["youtube_video_id"].fillna("").astype(str),
            "fb:" + fallback_song_key,
        ),
    )

    deduped = data.sort_values(
        ["artist_is_featured_on_track", "link_quality_score", "momentum_score", "views", "stream"],
        ascending=[True, False, False, False, False],
    )
    deduped = deduped.drop_duplicates(subset=["canonical_song_key"], keep="first")
    deduped = deduped.drop_duplicates(subset=["artist", "track"], keep="first")
    return deduped.reset_index(drop=True)


def _zscore(frame: pd.DataFrame) -> pd.DataFrame:
    std = frame.std(ddof=0).replace(0, 1)
    return (frame - frame.mean()) / std


def recommend_tracks(
    data: pd.DataFrame,
    seed_display_name: str | list[str] | dict[str, float],
    mood: str,
    spotify_weight: float,
    discovery_mode: float,
    top_k: int = 12,
    exclude_seed_tracks: bool = True,
    preferred_artist_weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    required_scoring_columns = {"display_name", "artist", "stream_norm", "views_norm", "popularity_norm", "momentum_score"}
    missing_columns = [col for col in [*FEATURE_COLUMNS, *required_scoring_columns] if col not in data.columns]
    if missing_columns:
        raise ValueError(f"Missing scoring columns: {', '.join(sorted(set(missing_columns)))}")

    valid_names = set(data["display_name"])
    seed_weights: dict[str, float] = {}

    if isinstance(seed_display_name, str):
        if seed_display_name not in valid_names:
            raise ValueError("Selected track was not found in the dataset.")
        seed_weights = {seed_display_name: 1.0}
    elif isinstance(seed_display_name, dict):
        for name, weight in seed_display_name.items():
            if name in valid_names:
                seed_weights[name] = float(weight or 0.0)
    else:
        for name in seed_display_name:
            if name in valid_names:
                seed_weights[str(name)] = seed_weights.get(str(name), 0.0) + 1.0

    seed_weights = {name: weight for name, weight in seed_weights.items() if weight > 0}
    if not seed_weights:
        raise ValueError("No valid seed tracks were selected.")

    total_weight = float(sum(seed_weights.values()))
    seed_weights = {name: (weight / total_weight) for name, weight in seed_weights.items()}

    features = _zscore(data[FEATURE_COLUMNS].astype(float)).fillna(0.0)
    target = pd.Series(0.0, index=features.columns)
    for seed_name, seed_weight in seed_weights.items():
        seed_index = int(data.index[data["display_name"] == seed_name][0])
        target += features.loc[seed_index] * seed_weight

    for feature_name, delta in MOOD_ADJUSTMENTS.get(mood, {}).items():
        if feature_name in target.index:
            target[feature_name] += delta

    similarity_raw = features.to_numpy() @ target.to_numpy()
    norms = np.linalg.norm(features.to_numpy(), axis=1) * np.linalg.norm(target.to_numpy())
    similarity = np.divide(similarity_raw, norms, out=np.zeros_like(similarity_raw), where=norms != 0)
    similarity_score = (similarity + 1) / 2.0

    spotify_weight = float(np.clip(spotify_weight, 0, 1))
    discovery_mode = float(np.clip(discovery_mode, 0, 1))

    stream_norm = pd.to_numeric(data["stream_norm"], errors="coerce").fillna(0.0)
    views_norm = pd.to_numeric(data["views_norm"], errors="coerce").fillna(0.0)
    popularity_norm = pd.to_numeric(data["popularity_norm"], errors="coerce").fillna(0.0)
    momentum_score = pd.to_numeric(data["momentum_score"], errors="coerce").fillna(0.0)

    platform_score = spotify_weight * stream_norm + (1 - spotify_weight) * views_norm
    discovery_score = (1 - discovery_mode) * popularity_norm + discovery_mode * (1 - popularity_norm)
    hits_preference = 1.0 - discovery_mode

    seed_set = set(seed_weights.keys())
    raw_artist_weights = {
        str(name).strip(): float(weight)
        for name, weight in (preferred_artist_weights or {}).items()
        if str(name).strip() and float(weight) > 0
    }
    if raw_artist_weights:
        total_artist_weight = float(sum(raw_artist_weights.values()))
        normalized_artist_weights = {k: (v / total_artist_weight) for k, v in raw_artist_weights.items()}
    else:
        normalized_artist_weights = {}

    artist_clean_series = (
        data["artist_clean"].astype(str)
        if "artist_clean" in data.columns
        else data["artist"].apply(_clean_text).astype(str)
    )
    weight_by_clean_artist = {_clean_text(name): weight for name, weight in normalized_artist_weights.items()}
    artist_weight_direct = artist_clean_series.map(weight_by_clean_artist).fillna(0.0).to_numpy(dtype=float)
    featured_norms_series = (
        data["featured_artist_norms"].fillna("").astype(str)
        if "featured_artist_norms" in data.columns
        else pd.Series([""] * len(data), index=data.index, dtype=str)
    )
    featured_lists = featured_norms_series.str.split("|")
    featured_weight = np.array(
        [sum(weight_by_clean_artist.get(name, 0.0) for name in names if name) for names in featured_lists],
        dtype=float,
    )
    artist_weight = np.clip(artist_weight_direct + 0.8 * featured_weight, 0.0, 2.0)
    low_popularity = 1.0 - popularity_norm.to_numpy(dtype=float)

    seed_bonus = np.where(data["display_name"].isin(seed_set), 0.02 + 0.26 * hits_preference, 0.0)
    artist_hits_bonus = artist_weight * (0.12 * hits_preference)
    artist_hidden_bonus = artist_weight * discovery_mode * low_popularity * (0.08 + 0.22 * similarity_score)
    artist_bonus = artist_hits_bonus + artist_hidden_bonus

    final_score = (
        0.58 * similarity_score
        + 0.16 * platform_score
        + 0.16 * momentum_score
        + 0.10 * discovery_score
        + seed_bonus
        + artist_bonus
    )

    scored = data.copy()
    scored["similarity_score"] = similarity_score
    scored["platform_score"] = platform_score
    scored["discovery_score"] = discovery_score
    scored["seed_bonus"] = seed_bonus
    scored["artist_bonus"] = artist_bonus
    scored["recommendation_score"] = final_score

    if exclude_seed_tracks:
        results = scored.loc[~scored["display_name"].isin(seed_weights.keys())]
    else:
        results = scored
    results = results.sort_values(["recommendation_score", "momentum_score"], ascending=False)
    results = _dedupe_ranked_candidates_by_links(results)
    if top_k <= 0:
        return results.head(0).reset_index(drop=True)
    return results.head(top_k).reset_index(drop=True)


def build_duration_playlist(
    recommendations: pd.DataFrame,
    target_minutes: int,
    tolerance_minutes: int = 3,
    candidate_limit: int = 140,
    max_tracks: int = 45,
) -> pd.DataFrame:
    if recommendations.empty:
        return recommendations.copy()

    candidate_limit = int(np.clip(candidate_limit, 1, 500))
    max_tracks = int(np.clip(max_tracks, 1, 120))
    tolerance_minutes = int(max(0, tolerance_minutes))
    target_minutes = int(max(1, target_minutes))

    candidates = recommendations.head(candidate_limit).copy().reset_index(drop=True)
    candidates = _dedupe_ranked_candidates_by_links(candidates).reset_index(drop=True)
    if "recommendation_score" not in candidates.columns:
        fallback_momentum = pd.to_numeric(candidates.get("momentum_score"), errors="coerce").fillna(0.0)
        candidates["recommendation_score"] = fallback_momentum

    durations_ms = pd.to_numeric(candidates.get("duration_ms"), errors="coerce").fillna(0)
    positive_ms = durations_ms[durations_ms > 0]
    fallback_ms = float(positive_ms.median() if not positive_ms.empty else 180_000)
    normalized_ms = durations_ms.where(durations_ms > 0, fallback_ms)
    duration_seconds = (normalized_ms / 1000).round().astype(int).clip(lower=60)
    recommendation_scores = pd.to_numeric(candidates["recommendation_score"], errors="coerce").fillna(0.0)

    target_seconds = int(target_minutes * 60)
    tolerance_seconds = int(max(0, tolerance_minutes) * 60)
    lower_bound = max(60, target_seconds - tolerance_seconds)
    upper_bound = target_seconds + tolerance_seconds
    max_sum_seconds = upper_bound + int(duration_seconds.max())

    # total_seconds -> (score_sum, selected_candidate_indexes)
    states: Dict[int, tuple[float, List[int]]] = {0: (0.0, [])}

    for idx, (duration, score) in enumerate(zip(duration_seconds.tolist(), recommendation_scores.tolist())):
        current_states = list(states.items())
        for sum_seconds, (score_sum, picked_indexes) in current_states:
            if len(picked_indexes) >= max_tracks:
                continue

            new_sum = sum_seconds + duration
            if new_sum > max_sum_seconds:
                continue

            new_score = score_sum + float(score)
            existing = states.get(new_sum)
            if existing is None or new_score > existing[0]:
                states[new_sum] = (new_score, picked_indexes + [idx])

    valid_states = [(sum_seconds, payload[0], payload[1]) for sum_seconds, payload in states.items() if payload[1]]
    if not valid_states:
        return candidates.head(0)

    in_range_states = [state for state in valid_states if lower_bound <= state[0] <= upper_bound]
    pool = in_range_states if in_range_states else valid_states

    best_sum_seconds, _, best_indexes = min(
        pool,
        key=lambda entry: (
            abs(entry[0] - target_seconds),
            -entry[1],
            entry[0] < target_seconds,
        ),
    )

    playlist = candidates.iloc[best_indexes].copy()
    playlist["playlist_target_minutes"] = round(best_sum_seconds / 60.0, 2)
    playlist = playlist.sort_values("recommendation_score", ascending=False).reset_index(drop=True)
    return playlist


def format_compact_number(value: float) -> str:
    value = float(value or 0.0)
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"
