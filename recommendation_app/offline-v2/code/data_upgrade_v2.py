from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
import runtime_config

BASELINE_DATASET_ID = runtime_config.BASELINE_DATASET_ID
EXPANSION_DATASET_ID = runtime_config.EXPANSION_DATASET_ID
_MERGE_CACHE_DIR = runtime_config.MERGE_CACHE_DIR


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    return out


def _series_or_default(df: pd.DataFrame, column: str, default: object = "") -> pd.Series:
    """Return a column as a Series aligned to df.index, even when missing."""
    if column in df.columns:
        series = df[column]
    else:
        series = pd.Series([default] * len(df), index=df.index)
    if not isinstance(series, pd.Series):
        series = pd.Series([series] * len(df), index=df.index)
    return series


def _resolve_dataset_csv(dataset_path: Path, dataset_id: str) -> list[Path]:
    csv_files = sorted(dataset_path.rglob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found under: {dataset_path}")

    if dataset_id == BASELINE_DATASET_ID:
        for candidate in csv_files:
            name = candidate.name.lower()
            if "spotify" in name and "youtube" in name:
                return [candidate]
        return [max(csv_files, key=lambda file_path: file_path.stat().st_size)]

    if dataset_id == EXPANSION_DATASET_ID:
        preferred: list[Path] = []
        for marker in ("high_popularity", "low_popularity"):
            for candidate in csv_files:
                if marker in candidate.name.lower():
                    preferred.append(candidate)
        if preferred:
            # preserve deterministic order high then low when possible
            preferred = sorted(preferred, key=lambda p: ("low" in p.name.lower(), p.name.lower()))
            return preferred

    return csv_files


def _resolve_cached_dataset_path(dataset_id: str) -> Path:
    slug_parts = dataset_id.split("/")
    if len(slug_parts) != 2:
        raise FileNotFoundError(f"Invalid dataset id format: {dataset_id}")

    cache_root = Path.home() / ".cache" / "kagglehub" / "datasets" / slug_parts[0] / slug_parts[1] / "versions"
    if not cache_root.exists():
        raise FileNotFoundError(f"No cached kagglehub versions found for: {dataset_id}")

    version_dirs = [path for path in cache_root.iterdir() if path.is_dir()]
    if not version_dirs:
        raise FileNotFoundError(f"No cached kagglehub version directories found for: {dataset_id}")

    def _version_key(path: Path) -> tuple[int, str]:
        try:
            return int(path.name), path.name
        except ValueError:
            return -1, path.name

    return sorted(version_dirs, key=_version_key)[-1]


def _extract_spotify_track_id_from_uri(uri: object) -> str | None:
    text = str(uri or "")
    match = re.search(r"spotify:track:([A-Za-z0-9]+)", text)
    return match.group(1) if match else None


def _extract_spotify_track_id_from_url(url: object) -> str | None:
    text = str(url or "")
    match = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]+)", text)
    return match.group(1) if match else None


def _extract_youtube_video_id(url: object) -> str | None:
    text = str(url or "").strip()
    if not text:
        return None

    def _clean_video_id(value: object) -> str | None:
        candidate = str(value or "").strip()
        candidate = candidate.split("?", 1)[0].split("&", 1)[0].split("#", 1)[0]
        return candidate if re.fullmatch(r"[A-Za-z0-9_-]{6,15}", candidate) else None

    parsed = urlparse(text)
    if not parsed.netloc and parsed.path:
        parsed = urlparse(f"https://{text}")

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
        match = re.search(pattern, text)
        if match:
            video_id = _clean_video_id(match.group(1))
            if video_id:
                return video_id

    if "/" not in text and "?" not in text and "&" not in text:
        return _clean_video_id(text)
    return None


def _to_watch_url(url: object) -> str:
    video_id = _extract_youtube_video_id(url)
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""


def _normalize_text(value: object) -> str:
    text = "" if pd.isna(value) else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.strip().lower()
    text = text.replace("’", "'").replace("‘", "'").replace("`", "'").replace("´", "'")
    text = text.replace("&", " and ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_artist(value: object) -> str:
    text = _normalize_text(value)
    text = re.sub(r"[^a-z0-9\s']+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" '")


def _normalize_track(value: object) -> str:
    text = _normalize_text(value)
    text = re.sub(r"\((?:[^)]*\bfeat\.?\b[^)]*)\)", "", text)
    text = re.sub(r"\((?:[^)]*\bft\.?\b[^)]*)\)", "", text)
    text = re.sub(r"\bfeat\.?\b.*$", "", text)
    text = re.sub(r"\bft\.?\b.*$", "", text)
    text = re.sub(
        r"\s*[-–:]\s*(?:\d{2,4}\s*)?(?:remaster(?:ed)?|radio edit|explicit|clean|live|version|mono|stereo|deluxe|edit|mix|remix).*$",
        "",
        text,
    )
    text = re.sub(
        r"\s+(?:\d{2,4}\s*)?(?:remaster(?:ed)?|radio edit|explicit|clean|live|version|mono|stereo|deluxe|edit|mix|remix)$",
        "",
        text,
    )
    text = re.sub(r"[^a-z0-9\s']+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" '")


def _extract_featured_from_track(track: object) -> list[str]:
    text = str(track or "")
    if not text:
        return []
    match = re.search(r"\((?:feat\.?|ft\.?)\s*([^)]+)\)", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"(?:feat\.?|ft\.?)\s+(.+)$", text, flags=re.IGNORECASE)
    if not match:
        return []
    block = match.group(1)
    parts = re.split(r",|&| and ", block, flags=re.IGNORECASE)
    return [part.strip() for part in parts if part and part.strip()]


def _split_artist_credits(text: object) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []

    tokens = [part.strip() for part in raw.split(",") if part and part.strip()]

    # Preserve common name pattern "Tyler, The Creator".
    merged: list[str] = []
    i = 0
    while i < len(tokens):
        current = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if current.lower() == "tyler" and nxt.lower() == "the creator":
            merged.append("Tyler, The Creator")
            i += 2
            continue
        merged.append(current)
        i += 1

    # De-duplicate while preserving order
    output: list[str] = []
    seen: set[str] = set()
    for name in merged:
        key = _normalize_artist(name)
        if key and key not in seen:
            seen.add(key)
            output.append(name.strip())
    return output


def _credits_to_json(names: Iterable[str], featured_norms: set[str]) -> str:
    roles: list[dict[str, str]] = []
    for name in names:
        norm = _normalize_artist(name)
        if not norm:
            continue
        role = "featured" if norm in featured_norms else "primary"
        roles.append({"name": name, "role": role})
    if roles and not any(item["role"] == "primary" for item in roles):
        roles[0]["role"] = "primary"
    return json.dumps(roles, ensure_ascii=False)


def _build_stable_owner_map(base: pd.DataFrame) -> dict[str, str]:
    owner_map: dict[str, str] = {}
    if "sid" not in base.columns:
        return owner_map

    grouped = base.dropna(subset=["sid"]).groupby("sid")
    for sid, grp in grouped:
        artists = [str(value).strip() for value in grp["artist"].tolist() if str(value).strip()]
        normed = {_normalize_artist(name) for name in artists if _normalize_artist(name)}
        if len(normed) == 1 and artists:
            owner_map[str(sid)] = artists[0]
    return owner_map


def _artist_credits_to_names(credits_json: object) -> list[str]:
    text = str(credits_json or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    names: list[str] = []
    for item in parsed:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if name:
                names.append(name)
    return names


def _merge_credit_roles(values: Iterable[str], owner: str) -> str:
    role_by_norm: dict[str, tuple[str, str]] = {}
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            role = str(item.get("role", "featured")).strip().lower() or "featured"
            norm = _normalize_artist(name)
            if not norm:
                continue
            existing = role_by_norm.get(norm)
            if existing is None:
                role_by_norm[norm] = (name, role)
            elif existing[1] != "primary" and role == "primary":
                role_by_norm[norm] = (name, "primary")

    owner_norm = _normalize_artist(owner)
    if owner_norm:
        role_by_norm[owner_norm] = (owner, "primary")

    merged = [{"name": name, "role": role} for _, (name, role) in sorted(role_by_norm.items(), key=lambda t: t[1][0].lower())]
    return json.dumps(merged, ensure_ascii=False)


def _build_baseline_frame(raw: pd.DataFrame) -> pd.DataFrame:
    base = _normalize_columns(raw)
    base = base.rename(columns={"streams": "stream"})
    uri_series = _series_or_default(base, "uri", "")
    base["sid"] = uri_series.apply(_extract_spotify_track_id_from_uri)
    if "url_spotify" in base.columns:
        sid_from_url = base["url_spotify"].apply(_extract_spotify_track_id_from_url)
        base["sid"] = base["sid"].fillna(sid_from_url)

    track_series = _series_or_default(base, "track", "")
    artist_series = _series_or_default(base, "artist", "")
    featured = track_series.apply(_extract_featured_from_track)
    credit_json = []
    for artist, feat_list in zip(artist_series, featured):
        all_names = [str(artist).strip()] + [name for name in feat_list if name]
        featured_norms = {_normalize_artist(name) for name in feat_list if _normalize_artist(name)}
        credit_json.append(_credits_to_json(all_names, featured_norms))

    base["artist_owner"] = artist_series.astype(str)
    base["artist_credits"] = credit_json
    base["artist_credit_names"] = base["artist_credits"].apply(
        lambda text: "|".join(sorted({_normalize_artist(name) for name in _artist_credits_to_names(text) if _normalize_artist(name)}))
    )
    base["normalized_artist"] = artist_series.apply(_normalize_artist)
    base["normalized_track"] = track_series.apply(_normalize_track)
    base["normalized_key"] = base["normalized_artist"] + "|||" + base["normalized_track"]
    youtube_series = _series_or_default(base, "url_youtube", "")
    base["youtube_video_id"] = youtube_series.apply(_extract_youtube_video_id)
    base["url_youtube"] = youtube_series.apply(_to_watch_url)

    for required_col in ("views", "likes", "comments", "stream"):
        if required_col not in base.columns:
            base[required_col] = 0
    base[["views", "likes", "comments", "stream"]] = base[["views", "likes", "comments", "stream"]].apply(
        pd.to_numeric, errors="coerce"
    ).fillna(0)
    base["channel"] = _series_or_default(base, "channel", "").fillna("").astype(str)

    channel_views = base.groupby("channel", as_index=False)["views"].sum().rename(columns={"views": "channel_total_views"})
    base = base.merge(channel_views, on="channel", how="left")
    base["channel_total_views"] = pd.to_numeric(_series_or_default(base, "channel_total_views", 0), errors="coerce").fillna(0)

    title_norm = _series_or_default(base, "title", "").fillna("").astype(str).apply(_normalize_text)
    channel_norm = base["channel"].apply(_normalize_text)
    owner_norm = base["artist_owner"].apply(_normalize_artist)
    credit_lists = base["artist_credit_names"].fillna("").astype(str).str.split("|")

    base["yt_has_video"] = base["youtube_video_id"].fillna("").astype(str).str.len() > 0
    base["yt_title_official_mv"] = title_norm.str.contains(r"official\\s+music\\s+video|official\\s+video", regex=True)
    base["yt_title_official_audio"] = title_norm.str.contains(r"official\\s+audio|\\baudio\\b", regex=True)

    channel_match = []
    for channel_text, owner_text, credit_names in zip(channel_norm.tolist(), owner_norm.tolist(), credit_lists.tolist()):
        matched = 0
        if owner_text and owner_text in channel_text:
            matched = 1
        if not matched:
            for name in credit_names:
                name_norm = _normalize_artist(name)
                if name_norm and name_norm in channel_text:
                    matched = 1
                    break
        channel_match.append(matched)
    base["yt_channel_artist_match"] = channel_match

    base["youtube_quality_score"] = (
        base["yt_has_video"].astype(int) * 3.0
        + base["yt_title_official_mv"].astype(int) * 2.0
        + base["yt_title_official_audio"].astype(int) * 1.0
        + pd.to_numeric(base["yt_channel_artist_match"], errors="coerce").fillna(0) * 1.5
        + np.log1p(pd.to_numeric(base["views"], errors="coerce").fillna(0)) * 0.15
        + np.log1p(pd.to_numeric(base["channel_total_views"], errors="coerce").fillna(0)) * 0.08
    )

    base["url_spotify"] = base["sid"].apply(
        lambda sid: f"https://open.spotify.com/track/{sid}" if isinstance(sid, str) and sid else ""
    )
    base["source_dataset"] = "baseline"
    base["source_tier"] = "baseline"
    return base


def _build_new_frame(raw_new: pd.DataFrame, baseline_ref: pd.DataFrame, stable_owner_map: dict[str, str]) -> pd.DataFrame:
    new = _normalize_columns(raw_new)
    new["sid"] = _series_or_default(new, "track_id", None).astype(object)
    sid_from_uri = _series_or_default(new, "uri", None).apply(_extract_spotify_track_id_from_uri)
    new["sid"] = new["sid"].where(new["sid"].astype(str).str.len() > 0, sid_from_uri)

    credits_raw = _series_or_default(new, "track_artist", "").apply(_split_artist_credits)
    track_name_series = _series_or_default(new, "track_name", "")

    owners: list[str] = []
    credits_json: list[str] = []
    credit_names_pipe: list[str] = []

    for sid, credit_names, track_name in zip(new["sid"], credits_raw, track_name_series):
        feat_norms = {_normalize_artist(name) for name in _extract_featured_from_track(track_name) if _normalize_artist(name)}
        if not credit_names:
            credit_names = [""]

        primary = [name for name in credit_names if _normalize_artist(name) not in feat_norms]
        owner = stable_owner_map.get(str(sid), "") if sid else ""
        if not owner:
            owner = primary[0] if primary else credit_names[0]

        roles_json = _credits_to_json(credit_names, feat_norms)
        owners.append(owner)
        credits_json.append(roles_json)
        credit_names_pipe.append(
            "|".join(sorted({_normalize_artist(name) for name in _artist_credits_to_names(roles_json) if _normalize_artist(name)}))
        )

    track_series = _series_or_default(new, "track_name", "").fillna("").astype(str)

    new["artist_owner"] = pd.Series(owners, index=new.index, dtype=str).fillna("")
    new["artist_credits"] = pd.Series(credits_json, index=new.index, dtype=str).fillna("")
    new["artist_credit_names"] = pd.Series(credit_names_pipe, index=new.index, dtype=str).fillna("")
    new["artist"] = new["artist_owner"].astype(str)
    new["track"] = track_series
    new["normalized_artist"] = new["artist_owner"].fillna("").astype(str).apply(_normalize_artist)
    new["normalized_track"] = track_series.apply(_normalize_track)
    new["normalized_key"] = new["normalized_artist"] + "|||" + new["normalized_track"]

    # enrich from baseline using sid first, normalized key second
    baseline_best_sid = baseline_ref.sort_values(
        ["youtube_quality_score", "views", "stream", "likes", "comments"],
        ascending=False,
    ).drop_duplicates(subset=["sid"], keep="first")
    baseline_best_key = baseline_ref.sort_values(
        ["youtube_quality_score", "views", "stream", "likes", "comments"],
        ascending=False,
    ).drop_duplicates(subset=["normalized_key"], keep="first")

    for col, default in {
        "sid": "",
        "url_youtube": "",
        "title": "",
        "views": np.nan,
        "likes": np.nan,
        "comments": np.nan,
        "stream": np.nan,
    }.items():
        if col not in baseline_best_sid.columns:
            baseline_best_sid[col] = default
    for col, default in {
        "normalized_key": "",
        "url_youtube": "",
        "title": "",
        "views": np.nan,
        "likes": np.nan,
        "comments": np.nan,
        "stream": np.nan,
    }.items():
        if col not in baseline_best_key.columns:
            baseline_best_key[col] = default

    sid_enrich_cols = ["sid", "url_youtube", "title", "views", "likes", "comments", "stream"]
    key_enrich_cols = ["normalized_key", "url_youtube", "title", "views", "likes", "comments", "stream"]

    sid_lookup = baseline_best_sid[sid_enrich_cols].rename(columns={
        "url_youtube": "url_youtube_sid",
        "title": "title_sid",
        "views": "views_sid",
        "likes": "likes_sid",
        "comments": "comments_sid",
        "stream": "stream_sid",
    })
    key_lookup = baseline_best_key[key_enrich_cols].rename(columns={
        "url_youtube": "url_youtube_key",
        "title": "title_key",
        "views": "views_key",
        "likes": "likes_key",
        "comments": "comments_key",
        "stream": "stream_key",
    })

    out = new.merge(sid_lookup, on="sid", how="left")
    out = out.merge(key_lookup, on="normalized_key", how="left")

    out["url_youtube"] = out["url_youtube_sid"].fillna("")
    out.loc[out["url_youtube"].eq(""), "url_youtube"] = out.loc[out["url_youtube"].eq(""), "url_youtube_key"].fillna("")

    out["title"] = out["title_sid"].fillna("")
    out.loc[out["title"].eq(""), "title"] = out.loc[out["title"].eq(""), "title_key"].fillna("")

    for col in ("views", "likes", "comments", "stream"):
        sid_col = f"{col}_sid"
        key_col = f"{col}_key"
        sid_series = pd.to_numeric(_series_or_default(out, sid_col, np.nan), errors="coerce")
        key_series = pd.to_numeric(_series_or_default(out, key_col, np.nan), errors="coerce")
        out[col] = sid_series.fillna(key_series)

    # Track-level fallback with ranking criteria when sid/key joins miss:
    # 1) direct video ID, 2) official title signals, 3) artist/channel match,
    # 4) video views, 5) channel reach proxy.
    baseline_candidates = baseline_ref.copy()
    baseline_candidates["url_youtube"] = _series_or_default(baseline_candidates, "url_youtube", "").fillna("").astype(str)
    baseline_candidates["youtube_video_id"] = baseline_candidates["url_youtube"].apply(_extract_youtube_video_id)
    baseline_candidates = baseline_candidates.loc[baseline_candidates["youtube_video_id"].fillna("").astype(str).str.len() > 0].copy()
    baseline_candidates["channel_norm"] = _series_or_default(baseline_candidates, "channel", "").fillna("").astype(str).apply(_normalize_text)
    baseline_candidates["candidate_artist_set"] = (
        _series_or_default(baseline_candidates, "artist_credit_names", "")
        .fillna("")
        .astype(str)
        .apply(lambda value: {token for token in value.split("|") if token})
    )
    baseline_candidates["candidate_artist_set"] = baseline_candidates["candidate_artist_set"].combine(
        _series_or_default(baseline_candidates, "normalized_artist", "").fillna("").astype(str).apply(
            lambda name: {name} if name else set()
        ),
        lambda left, right: set(left) | set(right),
    )

    missing_url_mask = out["url_youtube"].fillna("").eq("")
    if missing_url_mask.any() and not baseline_candidates.empty:
        fallback_indexes = out.index[missing_url_mask].tolist()
        for row_index in fallback_indexes:
            row_track = str(out.at[row_index, "normalized_track"] or "").strip()
            if not row_track:
                continue

            row_artist_set = {
                token
                for token in str(out.at[row_index, "artist_credit_names"] or "").split("|")
                if token
            }
            row_owner = str(out.at[row_index, "normalized_artist"] or "").strip()
            if row_owner:
                row_artist_set.add(row_owner)
            if not row_artist_set:
                continue

            track_candidates = baseline_candidates.loc[
                _series_or_default(baseline_candidates, "normalized_track", "").fillna("").astype(str) == row_track
            ].copy()
            if track_candidates.empty:
                continue

            def _candidate_score(candidate_row: pd.Series) -> float:
                candidate_set = candidate_row.get("candidate_artist_set", set()) or set()
                overlap = len(row_artist_set & set(candidate_set))
                owner_match = 1 if row_owner and row_owner in candidate_set else 0
                channel_norm = str(candidate_row.get("channel_norm", "") or "")
                channel_match = 1 if any(name and name in channel_norm for name in row_artist_set) else 0
                if owner_match == 0 and overlap == 0 and channel_match == 0:
                    return -1.0
                return (
                    float(candidate_row.get("youtube_quality_score", 0.0))
                    + owner_match * 2.0
                    + overlap * 1.25
                    + channel_match * 1.5
                )

            track_candidates["fallback_rank_score"] = track_candidates.apply(_candidate_score, axis=1)
            track_candidates = track_candidates.sort_values(
                ["fallback_rank_score", "youtube_quality_score", "views", "channel_total_views"],
                ascending=False,
            )
            best = track_candidates.iloc[0]
            if float(best.get("fallback_rank_score", -1.0)) < 0:
                continue

            out.at[row_index, "url_youtube"] = str(best.get("url_youtube", "") or "")
            if str(out.at[row_index, "title"] or "").strip() == "":
                out.at[row_index, "title"] = str(best.get("title", "") or "")
            for metric_col in ("views", "likes", "comments", "stream"):
                current_value = pd.to_numeric(out.at[row_index, metric_col], errors="coerce")
                if pd.isna(current_value):
                    out.at[row_index, metric_col] = pd.to_numeric(best.get(metric_col), errors="coerce")

    # fallback for new-only rows: estimate stream from Spotify popularity
    popularity = pd.to_numeric(_series_or_default(out, "track_popularity", 0), errors="coerce").fillna(0)
    out["stream"] = pd.to_numeric(out["stream"], errors="coerce").fillna(popularity * 100_000)
    out["views"] = pd.to_numeric(out["views"], errors="coerce").fillna(0)
    out["likes"] = pd.to_numeric(out["likes"], errors="coerce").fillna(0)
    out["comments"] = pd.to_numeric(out["comments"], errors="coerce").fillna(0)

    out["url_spotify"] = out["sid"].apply(lambda sid: f"https://open.spotify.com/track/{sid}" if isinstance(sid, str) and sid else "")
    out["url_youtube"] = out["url_youtube"].apply(_to_watch_url)

    for feature_col in ["danceability", "energy", "acousticness", "instrumentalness", "liveness", "valence", "speechiness", "tempo"]:
        if feature_col not in out.columns:
            out[feature_col] = np.nan

    out["source_dataset"] = "expansion"
    out["source_tier"] = out.get("source_tier", "unknown")
    return out


def _dedupe_combined(df: pd.DataFrame, stable_owner_map: dict[str, str]) -> pd.DataFrame:
    merged = df.copy()
    merged["sid"] = merged["sid"].fillna("").astype(str)
    merged["canonical_key"] = np.where(
        merged["sid"].str.len() > 0,
        "sp:" + merged["sid"],
        "fb:" + merged["normalized_key"].fillna("").astype(str),
    )

    merged["has_youtube"] = _series_or_default(merged, "url_youtube", "").astype(str).str.len() > 0
    merged["source_priority"] = np.where(merged.get("source_dataset", "") == "baseline", 2, 1)
    merged["row_score"] = (
        merged["has_youtube"].astype(int) * 1000
        + pd.to_numeric(merged.get("views"), errors="coerce").fillna(0) * 0.0001
        + pd.to_numeric(merged.get("stream"), errors="coerce").fillna(0) * 0.00005
        + merged["source_priority"]
    )

    representative = merged.sort_values(["row_score"], ascending=False).drop_duplicates(subset=["canonical_key"], keep="first")
    representative = representative.set_index("canonical_key", drop=False)

    for key, grp in merged.groupby("canonical_key"):
        if key not in representative.index:
            continue

        sid = str(grp["sid"].iloc[0])
        chosen_owner = representative.at[key, "artist_owner"]
        stable_owner = stable_owner_map.get(sid)
        if stable_owner:
            chosen_owner = stable_owner
        else:
            expansion_rows = grp.loc[grp.get("source_dataset", "").astype(str) == "expansion"]
            if not expansion_rows.empty:
                expansion_owner = str(expansion_rows.iloc[0].get("artist_owner", "")).strip()
                if expansion_owner:
                    chosen_owner = expansion_owner

        combined_credits = _merge_credit_roles(grp.get("artist_credits", pd.Series([], dtype=str)).tolist(), str(chosen_owner))
        representative.at[key, "artist_owner"] = str(chosen_owner)
        representative.at[key, "artist"] = str(chosen_owner)
        representative.at[key, "artist_credits"] = combined_credits
        representative.at[key, "artist_credit_names"] = "|".join(
            sorted({_normalize_artist(name) for name in _artist_credits_to_names(combined_credits) if _normalize_artist(name)})
        )

        if sid:
            representative.at[key, "url_spotify"] = f"https://open.spotify.com/track/{sid}"

    out = representative.reset_index(drop=True)
    out = out.drop(columns=[col for col in ["has_youtube", "source_priority", "row_score"] if col in out.columns])
    return out


def merge_two_kaggle_sources(baseline_raw: pd.DataFrame, expansion_raw: pd.DataFrame) -> pd.DataFrame:
    base = _build_baseline_frame(baseline_raw)
    stable_owner_map = _build_stable_owner_map(base)
    exp = _build_new_frame(expansion_raw, base, stable_owner_map)

    combined = pd.concat([base, exp], ignore_index=True, sort=False)
    combined = _dedupe_combined(combined, stable_owner_map)

    # required canonical columns for recommender
    if "streams" in combined.columns and "stream" not in combined.columns:
        combined = combined.rename(columns={"streams": "stream"})

    for col in ["views", "likes", "comments", "stream"]:
        if col not in combined.columns:
            combined[col] = 0
        combined[col] = pd.to_numeric(combined[col], errors="coerce").fillna(0)

    if "track" not in combined.columns and "track_name" in combined.columns:
        combined["track"] = combined["track_name"]
    if "artist" not in combined.columns and "artist_owner" in combined.columns:
        combined["artist"] = combined["artist_owner"]

    source_dataset = _series_or_default(combined, "source_dataset", "").fillna("").astype(str)
    source_tier = _series_or_default(combined, "source_tier", "")
    source_tier = source_tier.where(source_tier.fillna("").astype(str).str.len() > 0, np.where(source_dataset.eq("baseline"), "baseline", "unknown"))
    combined["source_tier"] = source_tier

    return combined.reset_index(drop=True)


def _source_fingerprint(paths: Iterable[Path]) -> str:
    parts: list[str] = []
    for path in sorted((Path(p).resolve() for p in paths), key=lambda p: str(p)):
        stat = path.stat()
        parts.append(f"{path}|{stat.st_size}|{getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1e9))}")
    joined = "||".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]


def _merge_cache_paths(cache_key: str) -> tuple[Path, Path]:
    _MERGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    parquet_path = _MERGE_CACHE_DIR / f"merged_{cache_key}.parquet"
    pickle_path = _MERGE_CACHE_DIR / f"merged_{cache_key}.pkl"
    return parquet_path, pickle_path


def _load_merged_cache(cache_key: str) -> pd.DataFrame | None:
    parquet_path, pickle_path = _merge_cache_paths(cache_key)
    if parquet_path.exists():
        try:
            return pd.read_parquet(parquet_path)
        except Exception:
            pass
    if pickle_path.exists():
        try:
            return pd.read_pickle(pickle_path)
        except Exception:
            pass
    return None


def _save_merged_cache(cache_key: str, merged: pd.DataFrame) -> None:
    parquet_path, pickle_path = _merge_cache_paths(cache_key)
    try:
        merged.to_parquet(parquet_path, index=False)
        return
    except Exception:
        pass
    try:
        merged.to_pickle(pickle_path)
    except Exception:
        return


def load_and_merge_from_kagglehub(
    baseline_dataset_id: str = BASELINE_DATASET_ID,
    expansion_dataset_id: str = EXPANSION_DATASET_ID,
) -> tuple[pd.DataFrame, str, str]:
    try:
        import kagglehub
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError("kagglehub is not installed. Run: pip install kagglehub") from exc

    try:
        baseline_path = _resolve_cached_dataset_path(baseline_dataset_id)
    except Exception:
        baseline_path = Path(kagglehub.dataset_download(baseline_dataset_id))

    try:
        expansion_path = _resolve_cached_dataset_path(expansion_dataset_id)
    except Exception:
        expansion_path = Path(kagglehub.dataset_download(expansion_dataset_id))

    baseline_csvs = _resolve_dataset_csv(baseline_path, baseline_dataset_id)
    expansion_csvs = _resolve_dataset_csv(expansion_path, expansion_dataset_id)
    cache_disabled = runtime_config.is_merge_cache_disabled()
    cache_key = _source_fingerprint([baseline_csvs[0], *expansion_csvs])

    if not cache_disabled:
        cached = _load_merged_cache(cache_key)
        if cached is not None and not cached.empty:
            source_path = f"{baseline_csvs[0]} + {', '.join(str(path) for path in expansion_csvs)}"
            dataset_path = f"{baseline_path} | {expansion_path}"
            return cached, source_path, dataset_path

    baseline_df = pd.read_csv(baseline_csvs[0])

    expansion_frames: list[pd.DataFrame] = []
    for csv_path in expansion_csvs:
        tier = "high" if "high" in csv_path.name.lower() else ("low" if "low" in csv_path.name.lower() else "unknown")
        expansion_frames.append(pd.read_csv(csv_path).assign(source_tier=tier, source_file=csv_path.name))
    expansion_df = pd.concat(expansion_frames, ignore_index=True, sort=False)

    merged = merge_two_kaggle_sources(baseline_df, expansion_df)
    if not cache_disabled:
        _save_merged_cache(cache_key, merged)

    source_path = f"{baseline_csvs[0]} + {', '.join(str(path) for path in expansion_csvs)}"
    dataset_path = f"{baseline_path} | {expansion_path}"
    return merged, source_path, dataset_path
