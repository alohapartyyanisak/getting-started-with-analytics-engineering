from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from datetime import date
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import runtime_config

_QUOTA_EXCEEDED_DAY: str | None = None

_VERSION_HINTS: dict[str, tuple[str, ...]] = {
    "acoustic": ("acoustic", "acustico", "unplugged"),
    "electric": ("electric",),
    "live": ("live", "ao vivo", "en vivo", "live session", "concert"),
    "remix": ("remix",),
    "instrumental": ("instrumental",),
    "audio": ("audio",),
    "karaoke": ("karaoke",),
    "cover": ("cover",),
    "demo": ("demo",),
}

_VERSION_CONFLICTS: dict[str, set[str]] = {
    "acoustic": {"electric"},
    "electric": {"acoustic"},
}

_NEGATIVE_TITLE_SIGNALS: dict[str, tuple[tuple[str, ...], float]] = {
    "reaction": (("reaction", "reacts", "first time hearing"), 4.0),
    "cover": (("cover",), 3.5),
    "karaoke": (("karaoke",), 4.0),
    "nightcore": (("nightcore",), 5.0),
    "8d": (("8d",), 4.0),
    "sped_up": (("sped up", "speed up"), 4.0),
    "slowed": (("slowed", "slow reverb", "reverb"), 3.5),
    "fan_edit": (("fan made", "fanmade", "amv", "edit"), 3.0),
    "tutorial": (("tutorial", "how to play", "lesson"), 4.0),
    "lyrics": (("lyric video", "lyrics", "lyric"), 2.6),
    "visualizer": (("visualizer",), 2.0),
}

_GENERIC_CHANNEL_SIGNALS: tuple[str, ...] = (
    "fans",
    "fan club",
    "fanclub",
    "lyrics",
    "letras",
    "music fans",
    "hits",
    "melhores",
    "best songs",
    "sertanejo",
    "radio",
    "fm",
    "clube fm",
    "producoes",
    "producao",
    "producao oficial",
)

_TRACK_STOPWORDS: set[str] = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "feat",
    "ft",
    "official",
    "video",
    "audio",
    "song",
    "music",
    "en",
    "de",
    "del",
    "da",
    "do",
    "dos",
    "das",
    "la",
    "el",
    "los",
    "las",
    "y",
    "e",
}

_CACHE_DB_PATH = runtime_config.YT_RESOLVER_CACHE_PATH
_CACHE_READY = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cache_connect() -> sqlite3.Connection:
    global _CACHE_READY
    _CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_CACHE_DB_PATH))
    if not _CACHE_READY:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS youtube_resolver_cache (
                yt_key TEXT PRIMARY KEY,
                watch_url TEXT,
                video_id TEXT,
                confidence TEXT,
                score REAL,
                status TEXT,
                reason TEXT,
                title TEXT,
                channel_title TEXT,
                api_source TEXT,
                resolved_at TEXT,
                expires_at TEXT,
                attempt_count INTEGER DEFAULT 1
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_youtube_resolver_cache_expires ON youtube_resolver_cache(expires_at)")
        conn.commit()
        _CACHE_READY = True
    return conn


def _cache_key(owner: str, track_name: str) -> str:
    owner_norm = _normalize_text(owner)
    track_core = _normalize_text(_strip_feature_text(track_name))
    version_tags = sorted(_extract_version_tags(track_name))
    version_tag = "+".join(version_tags) if version_tags else "default"
    return f"{owner_norm}|||{track_core}|||{version_tag}"


def _cache_ttl_days(status: str, confidence: str) -> int:
    status_norm = str(status or "").lower()
    conf_norm = str(confidence or "").lower()
    if status_norm in {"miss", "error"}:
        return 2
    if status_norm == "quota_blocked":
        return 1
    if conf_norm == "high":
        return 90
    if conf_norm == "medium":
        return 30
    return 7


def _cache_get(yt_key: str) -> dict[str, Any] | None:
    try:
        conn = _cache_connect()
        row = conn.execute(
            """
            SELECT watch_url, video_id, confidence, score, status, reason, title, channel_title, api_source, resolved_at, expires_at
            FROM youtube_resolver_cache
            WHERE yt_key = ?
            """,
            (yt_key,),
        ).fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    (
        watch_url,
        video_id,
        confidence,
        score,
        status,
        reason,
        title,
        channel_title,
        api_source,
        resolved_at,
        expires_at,
    ) = row
    try:
        exp_dt = datetime.fromisoformat(str(expires_at))
        if exp_dt < _utc_now():
            return None
    except Exception:
        return None
    return {
        "watch_url": str(watch_url or ""),
        "video_id": str(video_id or ""),
        "confidence": str(confidence or "Low"),
        "score": float(score or 0.0),
        "reason": str(reason or "cache hit"),
        "status": str(status or "hit"),
        "title": str(title or ""),
        "channel_title": str(channel_title or ""),
        "api_source": str(api_source or "cache"),
        "resolved_at": str(resolved_at or ""),
        "expires_at": str(expires_at or ""),
    }


def _cache_set(
    yt_key: str,
    *,
    watch_url: str,
    video_id: str,
    confidence: str,
    score: float,
    status: str,
    reason: str,
    title: str,
    channel_title: str,
    api_source: str,
) -> None:
    ttl_days = _cache_ttl_days(status=status, confidence=confidence)
    now = _utc_now()
    expires = now + timedelta(days=ttl_days)
    try:
        conn = _cache_connect()
        conn.execute(
            """
            INSERT INTO youtube_resolver_cache
                (yt_key, watch_url, video_id, confidence, score, status, reason, title, channel_title, api_source, resolved_at, expires_at, attempt_count)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(yt_key) DO UPDATE SET
                watch_url=excluded.watch_url,
                video_id=excluded.video_id,
                confidence=excluded.confidence,
                score=excluded.score,
                status=excluded.status,
                reason=excluded.reason,
                title=excluded.title,
                channel_title=excluded.channel_title,
                api_source=excluded.api_source,
                resolved_at=excluded.resolved_at,
                expires_at=excluded.expires_at,
                attempt_count=youtube_resolver_cache.attempt_count + 1
            """,
            (
                yt_key,
                watch_url,
                video_id,
                confidence,
                float(score or 0.0),
                status,
                reason,
                title,
                channel_title,
                api_source,
                now.isoformat(),
                expires.isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        return


def _api_budget_cap() -> int:
    return runtime_config.youtube_api_run_budget_units()


def _api_units_used_session() -> int:
    return runtime_config.youtube_api_units_used_session()


def _api_units_add_session(units: int) -> None:
    used = _api_units_used_session() + max(0, int(units))
    runtime_config.set_youtube_api_units_used_session(used)


def _api_units_for_url(url: str) -> int:
    if "youtube/v3/search" in url:
        return 100
    if "youtube/v3/videos" in url:
        return 1
    if "youtube/v3/channels" in url:
        return 1
    return 0


def _api_budget_would_exceed(units_to_add: int) -> bool:
    cap = _api_budget_cap()
    if cap <= 0:
        return False
    return (_api_units_used_session() + max(0, int(units_to_add))) > cap


def _normalize_text(value: object) -> str:
    text = str(value or "").strip().lower()
    # Fold accents: "boté" -> "bote"
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_feature_text(track: str) -> str:
    text = str(track or "")
    text = re.sub(r"\s*\((?:feat\.?|ft\.?).*?\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*(?:feat\.?|ft\.?)\s+.+$", "", text, flags=re.IGNORECASE)
    return text.strip()


def _official_signal(title: str) -> float:
    norm = _normalize_text(title)
    is_official_lyric = bool(
        re.search(
            r"\bofficial\s+lyric\s+video\b|\bofficial\s+lyrics\b|\blyric\s+video\b",
            norm,
        )
    )
    if is_official_lyric:
        # Keep below official video/audio so those are preferred when both match well.
        return 0.6
    if re.search(
        r"\bofficial\s+music\s+video\b|\bofficial\s+video\b|\bofficial\s+mv\b|\bvideo\s+oficial\b|\bclipe\s+oficial\b|\bvideoclipe\s+oficial\b",
        norm,
    ):
        return 2.4
    if re.search(r"\bofficial\s+audio\b|\baudio\s+oficial\b", norm):
        return 2.0
    if re.search(r"\bofficial\b|\boficial\b", norm):
        return 1.2
    if re.search(r"\baudio\b", norm):
        return 0.8
    return 0.0


def _extract_artist_tokens(owner: str, artist_credits_json: str) -> set[str]:
    def _expand_aliases(name_norm: str) -> set[str]:
        if not name_norm:
            return set()
        aliases = {name_norm}
        swaps = [
            (" and ", " e "),
            (" and ", " y "),
            (" e ", " and "),
            (" e ", " y "),
            (" y ", " and "),
            (" y ", " e "),
        ]
        for left, right in swaps:
            if left in name_norm:
                aliases.add(name_norm.replace(left, right))
        parts = re.split(r"\s+(?:and|e|y)\s+|,", name_norm)
        for part in parts:
            clean = part.strip()
            if len(clean) >= 3:
                aliases.add(clean)
        return aliases

    tokens = set()

    owner_norm = _normalize_text(owner)
    if owner_norm:
        tokens |= _expand_aliases(owner_norm)

    raw = str(artist_credits_json or "").strip()
    if not raw:
        return tokens

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = []

    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                name_norm = _normalize_text(item.get("name", ""))
                if name_norm:
                    tokens |= _expand_aliases(name_norm)
    return tokens


def _channel_match_score(channel_title: str, artist_tokens: set[str]) -> float:
    channel_norm = _normalize_text(channel_title)
    if not channel_norm or not artist_tokens:
        return 0.0
    if any(token and token in channel_norm for token in artist_tokens):
        return 1.5
    return 0.0


def _title_match_score(title: str, owner: str, track: str) -> float:
    title_norm = _normalize_text(title)
    owner_norm = _normalize_text(owner)
    track_norm = _normalize_text(track)
    track_core = _normalize_text(_strip_feature_text(track))

    score = 0.0
    if owner_norm and owner_norm in title_norm:
        score += 1.0
    if track_core and track_core in title_norm:
        score += 1.0
    elif track_norm and track_norm in title_norm:
        score += 0.5
    return score


def _artist_identity_score(title: str, channel_title: str, owner: str, artist_tokens: set[str]) -> float:
    title_norm = _normalize_text(title)
    channel_norm = _normalize_text(channel_title)
    owner_norm = _normalize_text(owner)

    score = 0.0
    if owner_norm:
        if owner_norm in title_norm:
            score += 1.5
        if owner_norm in channel_norm:
            score += 1.5

    other_tokens = {token for token in artist_tokens if token and token != owner_norm}
    if other_tokens:
        title_hits = sum(1 for token in other_tokens if token in title_norm)
        channel_hits = sum(1 for token in other_tokens if token in channel_norm)
        score += min(1.0, 0.35 * float(title_hits))
        score += min(1.0, 0.35 * float(channel_hits))

    return min(4.0, score)


def _token_overlap_score(track_text: str, title_text: str) -> float:
    def _token_forms(token: str) -> set[str]:
        out = {token}
        if token.endswith("s") and len(token) > 3:
            out.add(token[:-1])
        return out

    track_tokens = [token for token in _normalize_text(track_text).split() if token]
    if not track_tokens:
        return 0.0
    title_tokens: set[str] = set()
    for token in _normalize_text(title_text).split():
        if not token:
            continue
        title_tokens |= _token_forms(token)
    if not title_tokens:
        return 0.0
    overlap = 0
    for token in track_tokens:
        if _token_forms(token) & title_tokens:
            overlap += 1
    return overlap / max(1, len(track_tokens))


def _significant_tokens(text: str) -> list[str]:
    tokens = [token for token in _normalize_text(text).split() if token]
    return [token for token in tokens if len(token) >= 3 and token not in _TRACK_STOPWORDS and not token.isdigit()]


def _track_token_coverage(track_text: str, title_text: str) -> float:
    track_tokens = _significant_tokens(track_text)
    if not track_tokens:
        return 0.0
    title_tokens = set(_normalize_text(title_text).split())
    if not title_tokens:
        return 0.0
    overlap = sum(1 for token in track_tokens if token in title_tokens)
    return overlap / max(1, len(track_tokens))


def _track_anchor_quality(track_text: str, title_text: str) -> tuple[float, bool]:
    track_norm = _normalize_text(track_text)
    title_norm = _normalize_text(title_text)
    coverage = _track_token_coverage(track_text, title_text)
    has_phrase = bool(track_norm and track_norm in title_norm)
    track_tokens = _significant_tokens(track_text)
    # Single-token titles (e.g., "Touch") should pass when token is present.
    has_single_token_anchor = bool(len(track_tokens) == 1 and track_tokens[0] in title_norm.split())
    is_strong = has_phrase or coverage >= 0.60 or has_single_token_anchor
    return coverage, is_strong


def _extract_version_tags(text: str) -> set[str]:
    norm = _normalize_text(text)
    tags: set[str] = set()
    for canonical, patterns in _VERSION_HINTS.items():
        for pattern in patterns:
            if re.search(rf"(^|\s){re.escape(pattern)}(\s|$)", norm):
                tags.add(canonical)
                break
    return tags


def _version_alignment_score(request_text: str, candidate_title: str) -> float:
    request_tags = _extract_version_tags(request_text)
    candidate_tags = _extract_version_tags(candidate_title)

    if not request_tags:
        # No explicit version requested: lightly penalize niche variants.
        extra_variant = candidate_tags & {"acoustic", "electric", "live", "remix", "instrumental", "karaoke", "cover", "demo", "audio"}
        return -0.6 * float(len(extra_variant))

    score = 0.0
    matched = request_tags & candidate_tags
    missing = request_tags - candidate_tags
    score += 2.4 * float(len(matched))
    score -= 3.2 * float(len(missing))

    for tag in request_tags:
        conflicts = _VERSION_CONFLICTS.get(tag, set())
        if conflicts & candidate_tags:
            score -= 3.0
    return score


def _contains_phrase(text: str, phrase: str) -> bool:
    text_norm = _normalize_text(text)
    phrase_norm = _normalize_text(phrase)
    if not text_norm or not phrase_norm:
        return False
    return re.search(rf"(^|\s){re.escape(phrase_norm)}(\s|$)", text_norm) is not None


def _negative_title_penalty(request_text: str, candidate_title: str) -> float:
    request_norm = _normalize_text(request_text)
    request_tags = _extract_version_tags(request_text)

    def _allowed(signal_key: str) -> bool:
        if signal_key == "lyrics":
            return "lyric" in request_norm or "lyrics" in request_norm
        if signal_key == "visualizer":
            return "visualizer" in request_norm
        if signal_key == "sped_up":
            return "sped up" in request_norm or "speed up" in request_norm
        if signal_key == "8d":
            return "8d" in request_norm
        if signal_key in {"cover", "karaoke", "nightcore", "tutorial", "reaction"}:
            return signal_key in request_norm
        if signal_key == "slowed":
            return "slowed" in request_norm or "reverb" in request_norm
        if signal_key == "fan_edit":
            return "edit" in request_norm
        return signal_key in request_tags

    penalty = 0.0
    for signal_key, (patterns, weight) in _NEGATIVE_TITLE_SIGNALS.items():
        if _allowed(signal_key):
            continue
        if any(_contains_phrase(candidate_title, pattern) for pattern in patterns):
            penalty += float(weight)
    return -penalty


def _channel_quality_adjustment(channel_title: str, artist_tokens: set[str], views: float) -> float:
    channel_norm = _normalize_text(channel_title)
    if not channel_norm:
        return -1.0

    score = 0.0
    has_artist_match = any(token and token in channel_norm for token in artist_tokens)
    has_generic_signal = any(_contains_phrase(channel_norm, marker) for marker in _GENERIC_CHANNEL_SIGNALS)
    has_official_signal = any(
        _contains_phrase(channel_norm, marker)
        for marker in ("official", "vevo", "topic")
    )

    if has_artist_match:
        score += 1.4
    else:
        score -= 0.6

    if has_official_signal:
        score += 0.8

    if has_generic_signal:
        score -= 2.4

    if not has_artist_match and views < 50_000:
        score -= 3.0
    elif not has_artist_match and views < 250_000:
        score -= 1.4

    return score


def _is_generic_channel(channel_title: str) -> bool:
    channel_norm = _normalize_text(channel_title)
    if not channel_norm:
        return False
    return any(_contains_phrase(channel_norm, marker) for marker in _GENERIC_CHANNEL_SIGNALS)


def _is_lyric_title(title: str) -> bool:
    title_norm = _normalize_text(title)
    return bool(
        re.search(
            r"\bofficial\s+lyric\s+video\b|\bofficial\s+lyrics\b|\blyric\s+video\b|\blyrics\b|\blyric\b",
            title_norm,
        )
    )


def _live_intent_adjustment(
    request_track: str,
    candidate_title: str,
    channel_match: float,
    official_signal: float,
    artist_identity: float,
    views: float,
) -> float:
    request_tags = _extract_version_tags(request_track)
    if "live" not in request_tags:
        return 0.0

    title_norm = _normalize_text(candidate_title)
    candidate_tags = _extract_version_tags(candidate_title)
    has_live = "live" in candidate_tags
    trusted_live = bool(
        has_live
        and (
            channel_match > 0.0
            or official_signal >= 2.0
            or artist_identity >= 1.5
            or views >= 10_000
        )
    )

    if has_live and trusted_live:
        return 3.0
    if has_live and not trusted_live:
        # Discourage low-trust live captures despite keyword match.
        return -1.5
    # Stronger penalty for non-live results when live was requested.
    # DVD is not auto-penalized since many older live recordings are published from DVD.
    return -3.5


def _should_reject_low_trust_candidate(
    views: float,
    channel_match: float,
    official_signal: float,
    artist_identity: float,
    anchor_coverage: float,
) -> bool:
    if channel_match > 0.0 or official_signal >= 2.0:
        return False
    if views < 200:
        return True
    if views < 1_000 and artist_identity < 2.0:
        return True
    if views < 5_000 and anchor_coverage < 0.80 and artist_identity < 1.5:
        return True
    return False


def _build_query_variants(owner: str, track_name: str, limit: int = 3) -> list[str]:
    core = _strip_feature_text(track_name)
    tags = _extract_version_tags(track_name)

    candidates = [
        f"{owner} {track_name} official",
        f"{owner} {core or track_name} official",
        f"{owner} {track_name}",
    ]

    if tags:
        for tag in sorted(tags):
            if tag in {"acoustic", "electric", "live", "remix", "instrumental"}:
                candidates.append(f"{owner} {core or track_name} {tag} official")
                if tag == "live":
                    candidates.append(f"{owner} {core or track_name} ao vivo")
                    candidates.append(f"{owner} {core or track_name} en vivo")

    candidates.append(f"{owner} {core or track_name} official audio")

    deduped: list[str] = []
    seen: set[str] = set()
    for query in candidates:
        clean = re.sub(r"\s+", " ", query).strip()
        key = _normalize_text(clean)
        if not clean or key in seen:
            continue
        seen.add(key)
        deduped.append(clean)
        if len(deduped) >= max(1, int(limit)):
            break
    return deduped


def _is_video_available_oembed(video_id: str, timeout: int = 6) -> bool:
    watch_url = _build_watch_url(video_id)
    oembed_url = "https://www.youtube.com/oembed?" + urlencode(
        {
            "url": watch_url,
            "format": "json",
        }
    )
    request = Request(
        oembed_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)) < 400
    except Exception:
        return False


def _confidence_label(score: float, candidate: dict[str, Any] | None = None) -> str:
    if candidate:
        anchor = float(candidate.get("anchor_coverage", 0.0) or 0.0)
        artist_identity = float(candidate.get("artist_identity", 0.0) or 0.0)
        official_signal = float(candidate.get("official_signal", 0.0) or 0.0)
        low_trust = bool(candidate.get("low_trust", False))
        if (score >= 10.0 and anchor >= 0.60 and artist_identity >= 1.5 and official_signal >= 1.2 and not low_trust):
            return "High"
        if score >= 6.0 and anchor >= 0.45 and not low_trust:
            return "Medium"
        return "Low"

    if score >= 10.0:
        return "High"
    if score >= 6.0:
        return "Medium"
    return "Low"


def _http_get_json(url: str, timeout: int = 8) -> dict[str, Any]:
    units = _api_units_for_url(url)
    if _api_budget_would_exceed(units):
        raise RuntimeError("YouTube API budget guard: insufficient units for request")
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            _api_units_add_session(units)
            return payload
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        detail = f"HTTPError {exc.code}"
        if body:
            detail += f" {body}"
        raise RuntimeError(detail) from exc


def _build_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _today_key() -> str:
    return date.today().isoformat()


def _quota_lock_active() -> bool:
    return _QUOTA_EXCEEDED_DAY == _today_key()


def _mark_quota_exceeded_today() -> None:
    global _QUOTA_EXCEEDED_DAY
    _QUOTA_EXCEEDED_DAY = _today_key()


def _looks_like_quota_exceeded(error: object) -> bool:
    text = str(error or "").lower()
    return (
        "quota" in text
        and "exceed" in text
    ) or "quotaexceeded" in text or "dailylimitexceeded" in text


def _iter_video_renderers(node: Any):
    if isinstance(node, dict):
        if "videoRenderer" in node and isinstance(node["videoRenderer"], dict):
            yield node["videoRenderer"]
        for value in node.values():
            yield from _iter_video_renderers(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_video_renderers(item)


def _text_from_runs(value: Any) -> str:
    if isinstance(value, dict):
        if "simpleText" in value:
            return str(value.get("simpleText", "")).strip()
        runs = value.get("runs")
        if isinstance(runs, list):
            return "".join(str(run.get("text", "")) for run in runs if isinstance(run, dict)).strip()
    return ""


def _parse_view_count_text(text: str) -> float:
    raw = str(text or "").strip().lower().replace("views", "").replace("view", "").strip()
    raw = raw.replace(",", "")
    match = re.match(r"^([0-9]*\.?[0-9]+)\s*([kmb]?)$", raw)
    if not match:
        return 0.0
    value = float(match.group(1))
    suffix = match.group(2)
    if suffix == "k":
        return value * 1_000
    if suffix == "m":
        return value * 1_000_000
    if suffix == "b":
        return value * 1_000_000_000
    return value


def _extract_initial_data_from_html(html: str) -> dict[str, Any]:
    patterns = [
        r"var ytInitialData = (\{.*?\});",
        r"ytInitialData\s*=\s*(\{.*?\});",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.DOTALL)
        if not match:
            continue
        payload = match.group(1)
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            continue
    return {}


def _resolve_from_youtube_search_page(
    artist_owner: str,
    track: str,
    artist_credits_json: str = "",
    timeout: int = 8,
) -> dict[str, Any]:
    owner = str(artist_owner or "").strip()
    track_name = str(track or "").strip()
    artist_tokens = _extract_artist_tokens(owner, artist_credits_json)
    track_core = _strip_feature_text(track_name)

    query_variants = _build_query_variants(owner, track_name, limit=6)
    if not query_variants:
        return {
            "watch_url": "",
            "video_id": "",
            "confidence": "Low",
            "score": 0.0,
            "reason": "no query variants",
            "candidates": [],
            "cache_hit": False,
            "api_source": "html_fallback",
        }

    prelim_candidates: list[dict[str, Any]] = []
    seen_video_ids: set[str] = set()
    fetch_error: Exception | None = None

    for query_rank, query in enumerate(query_variants):
        search_url = "https://www.youtube.com/results?" + urlencode({"search_query": query})
        request = Request(
            search_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                html = response.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            fetch_error = exc
            continue

        initial_data = _extract_initial_data_from_html(html)
        if not initial_data:
            continue

        for video in _iter_video_renderers(initial_data):
            video_id = str(video.get("videoId", "")).strip()
            if not video_id or video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)

            title = _text_from_runs(video.get("title", {}))
            channel_title = _text_from_runs(video.get("ownerText", {}))
            view_text = _text_from_runs(video.get("viewCountText", {}))
            views = _parse_view_count_text(view_text)

            title_overlap = _token_overlap_score(track_core or track_name, title)
            anchor_coverage, strong_anchor = _track_anchor_quality(track_core or track_name, title)
            query_boost = max(0.0, 0.8 - 0.2 * float(query_rank))
            official_signal = _official_signal(title)
            channel_match = _channel_match_score(channel_title, artist_tokens)
            title_match = _title_match_score(title, owner, track_core)
            artist_identity = _artist_identity_score(title, channel_title, owner, artist_tokens)
            generic_channel = _is_generic_channel(channel_title)
            lyric_title = _is_lyric_title(title)
            low_trust = (
                artist_identity < 0.75
                and channel_match <= 0.0
                and official_signal <= 0.0
                and anchor_coverage < 0.70
            )
            reject_low_trust = _should_reject_low_trust_candidate(
                views=views,
                channel_match=channel_match,
                official_signal=official_signal,
                artist_identity=artist_identity,
                anchor_coverage=anchor_coverage,
            )
            if reject_low_trust:
                continue

            score = 3.0
            score += official_signal * 2.0
            score += channel_match
            score += title_match
            score += artist_identity * 2.2
            score += title_overlap * 3.2
            score += anchor_coverage * 4.0
            score += _version_alignment_score(track_name, title)
            score += _negative_title_penalty(track_name, title)
            score += _channel_quality_adjustment(channel_title, artist_tokens, views)
            score += query_boost
            score += _live_intent_adjustment(
                request_track=track_name,
                candidate_title=title,
                channel_match=channel_match,
                official_signal=official_signal,
                artist_identity=artist_identity,
                views=views,
            )
            if artist_identity < 0.75:
                score -= 6.0
            if generic_channel and channel_match <= 0.0 and views < 5_000 and official_signal < 2.0:
                score -= 7.0
            if views < 1_000 and channel_match <= 0.0 and official_signal < 2.0 and artist_identity < 1.5:
                score -= 6.0
            if lyric_title and "lyric" not in _normalize_text(track_name):
                score -= 3.0
            if title_overlap < 0.40:
                score -= 4.0
            if anchor_coverage < 0.34 and not strong_anchor:
                score -= 8.0
            if low_trust:
                score -= 6.0
            if track_core and _normalize_text(track_core) in _normalize_text(title):
                score += 1.8
            score += math.log1p(max(0.0, views)) * 0.22

            prelim_candidates.append(
                {
                    "video_id": video_id,
                    "watch_url": _build_watch_url(video_id),
                    "title": title,
                    "channel_title": channel_title,
                    "views": views,
                    "channel_reach_proxy": 0.0,
                    "title_overlap": title_overlap,
                    "anchor_coverage": anchor_coverage,
                    "artist_identity": artist_identity,
                    "official_signal": official_signal,
                    "generic_channel": generic_channel,
                    "lyric_title": lyric_title,
                    "low_trust": low_trust,
                    "reject_low_trust": reject_low_trust,
                    "query_rank": query_rank,
                    "score": score,
                }
            )

    if not prelim_candidates:
        return {
            "watch_url": "",
            "video_id": "",
            "confidence": "Low",
            "score": 0.0,
            "reason": f"search page request failed: {fetch_error}" if fetch_error else "no videoRenderer candidates",
            "candidates": [],
            "cache_hit": False,
            "api_source": "html_fallback",
        }

    prelim_candidates.sort(
        key=lambda row: (
            float(row.get("score", 0.0)),
            float(row.get("official_signal", 0.0)),
            float(row.get("title_overlap", 0.0)),
            float(row.get("views", 0.0)),
        ),
        reverse=True,
    )
    ranked_candidates: list[dict[str, Any]] = []
    for candidate in prelim_candidates[:10]:
        video_id = str(candidate.get("video_id", "")).strip()
        candidate["available"] = _is_video_available_oembed(video_id, timeout=timeout) if video_id else False
        if candidate["available"]:
            ranked_candidates.append(candidate)

    if not ranked_candidates:
        return {
            "watch_url": "",
            "video_id": "",
            "confidence": "Low",
            "score": 0.0,
            "reason": "no available candidates after validation",
            "candidates": prelim_candidates[:10],
            "cache_hit": False,
            "api_source": "html_fallback",
        }

    ranked_candidates.sort(
        key=lambda row: (
            float(row.get("score", 0.0)),
            float(row.get("official_signal", 0.0)),
            float(row.get("title_overlap", 0.0)),
            float(row.get("views", 0.0)),
        ),
        reverse=True,
    )

    best = ranked_candidates[0]
    if bool(best.get("low_trust", False)):
        trusted = [row for row in ranked_candidates if not bool(row.get("low_trust", False))]
        if trusted and float(trusted[0].get("score", 0.0)) >= float(best.get("score", 0.0)) - 3.0:
            best = trusted[0]
    best_score = float(best.get("score", 0.0))
    return {
        "watch_url": str(best.get("watch_url", "")),
        "video_id": str(best.get("video_id", "")),
        "confidence": _confidence_label(best_score, best),
        "score": best_score,
        "reason": (
            f"{best.get('title', '')} | {best.get('channel_title', '')} | "
            f"views={int(float(best.get('views', 0.0))):,}"
        ),
        "candidates": ranked_candidates,
        "cache_hit": False,
        "api_source": "html_fallback",
    }


def resolve_best_youtube_live(
    artist_owner: str,
    track: str,
    artist_credits_json: str = "",
    max_results: int = 10,
    max_query_variants: int = 2,
    timeout: int = 8,
) -> dict[str, Any]:
    """
    Resolve a YouTube watch URL from live YouTube Data API search, ranked by:
    1) direct video ID
    2) official title signals
    3) artist/channel match
    4) higher video views
    5) higher channel reach proxy (subscriber count)
    """

    api_key = runtime_config.youtube_api_key()
    owner = str(artist_owner or "").strip()
    track_name = str(track or "").strip()
    yt_key = _cache_key(owner, track_name)

    # 1) cache-first
    cached = _cache_get(yt_key)
    if cached is not None:
        return {
            "watch_url": cached["watch_url"],
            "video_id": cached["video_id"],
            "confidence": cached["confidence"],
            "score": float(cached["score"]),
            "reason": f"cache hit: {cached.get('reason', '')}",
            "candidates": [],
            "cache_hit": True,
            "api_source": str(cached.get("api_source", "cache")),
        }

    def _cache_and_return(payload: dict[str, Any], status: str | None = None) -> dict[str, Any]:
        watch_url = str(payload.get("watch_url", "") or "")
        confidence = str(payload.get("confidence", "Low") or "Low")
        if status is None:
            if not watch_url:
                status = "miss"
            elif confidence == "Low":
                status = "low_confidence"
            else:
                status = "hit"
        _cache_set(
            yt_key,
            watch_url=watch_url,
            video_id=str(payload.get("video_id", "") or ""),
            confidence=confidence,
            score=float(payload.get("score", 0.0) or 0.0),
            status=status,
            reason=str(payload.get("reason", "") or ""),
            title=str((payload.get("candidates", [{}]) or [{}])[0].get("title", "") if payload.get("candidates") else ""),
            channel_title=str((payload.get("candidates", [{}]) or [{}])[0].get("channel_title", "") if payload.get("candidates") else ""),
            api_source=str(payload.get("api_source", "unknown") or "unknown"),
        )
        payload["cache_hit"] = False
        return payload

    if not owner or not track_name:
        return _cache_and_return(
            {
                "watch_url": "",
                "video_id": "",
                "confidence": "Low",
                "score": 0.0,
                "reason": "missing artist/track input",
                "candidates": [],
                "api_source": "none",
            },
            status="error",
        )

    if _quota_lock_active():
        return _cache_and_return(
            {
                "watch_url": "",
                "video_id": "",
                "confidence": "Low",
                "score": 0.0,
                "reason": "YouTube API daily quota exceeded today. Skipping API calls until tomorrow.",
                "candidates": [],
                "api_source": "quota_lock",
            },
            status="quota_blocked",
        )

    # 2) if no API key, fallback mode
    if not api_key:
        fallback = _resolve_from_youtube_search_page(owner, track_name, artist_credits_json, timeout=timeout)
        return _cache_and_return(fallback)

    # 3) per-track budget guard for API path (search + videos + channels)
    if _api_budget_would_exceed(102):
        fallback = _resolve_from_youtube_search_page(owner, track_name, artist_credits_json, timeout=timeout)
        fallback["reason"] = "YouTube API budget guard: fallback to search-page resolver"
        return _cache_and_return(fallback, status="quota_blocked")

    # 4) reduce API fan-out (1-2 variants)
    query_limit = max(1, min(2, int(max_query_variants)))
    query_variants = _build_query_variants(owner, track_name, limit=query_limit)
    video_ids: list[str] = []
    query_rank_by_video_id: dict[str, int] = {}
    search_error: Exception | None = None

    for query_rank, query in enumerate(query_variants):
        search_params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": min(max(10, int(max_results)), 25),
            "key": api_key,
        }
        search_url = "https://www.googleapis.com/youtube/v3/search?" + urlencode(search_params)
        try:
            search_payload = _http_get_json(search_url, timeout=timeout)
        except Exception as exc:
            if _looks_like_quota_exceeded(exc):
                _mark_quota_exceeded_today()
                return _cache_and_return(
                    {
                        "watch_url": "",
                        "video_id": "",
                        "confidence": "Low",
                        "score": 0.0,
                        "reason": "YouTube API daily quota exceeded today. Skipping API calls until tomorrow.",
                        "candidates": [],
                        "api_source": "youtube_api",
                    },
                    status="quota_blocked",
                )
            search_error = exc
            continue

        for item in search_payload.get("items", []):
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id", {})
            if not isinstance(raw_id, dict):
                continue
            video_id = str(raw_id.get("videoId", "")).strip()
            if not video_id:
                continue
            if video_id not in query_rank_by_video_id:
                video_ids.append(video_id)
                query_rank_by_video_id[video_id] = query_rank
            if len(video_ids) >= max(12, int(max_results) * 2):
                break
        if len(video_ids) >= max(12, int(max_results) * 2):
            break
        # 5) stop extra retries when first query yields no usable IDs
        if query_rank == 0 and not video_ids:
            break

    if not video_ids:
        fallback = _resolve_from_youtube_search_page(owner, track_name, artist_credits_json, timeout=timeout)
        if not fallback.get("watch_url"):
            fallback["reason"] = f"search request failed: {search_error}" if search_error else "no search candidates"
        return _cache_and_return(fallback)

    video_params = {
        "part": "snippet,statistics,status",
        "id": ",".join(video_ids),
        "maxResults": len(video_ids),
        "key": api_key,
    }
    video_url = "https://www.googleapis.com/youtube/v3/videos?" + urlencode(video_params)
    try:
        video_payload = _http_get_json(video_url, timeout=timeout)
    except Exception as exc:
        if _looks_like_quota_exceeded(exc):
            _mark_quota_exceeded_today()
            return _cache_and_return(
                {
                    "watch_url": "",
                    "video_id": "",
                    "confidence": "Low",
                    "score": 0.0,
                    "reason": "YouTube API daily quota exceeded today. Skipping API calls until tomorrow.",
                    "candidates": [],
                    "api_source": "youtube_api",
                },
                status="quota_blocked",
            )
        fallback = _resolve_from_youtube_search_page(owner, track_name, artist_credits_json, timeout=timeout)
        if not fallback.get("watch_url"):
            fallback["reason"] = f"videos request failed: {exc}"
        return _cache_and_return(fallback)

    video_items = video_payload.get("items", [])
    if not video_items:
        fallback = _resolve_from_youtube_search_page(owner, track_name, artist_credits_json, timeout=timeout)
        if not fallback.get("watch_url"):
            fallback["reason"] = "no details for video IDs"
        return _cache_and_return(fallback)

    channel_ids: list[str] = []
    channel_title_by_id: dict[str, str] = {}
    stats_by_video_id: dict[str, tuple[float, str, str, bool]] = {}

    for item in video_items:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("id", "")).strip()
        if not video_id:
            continue
        snippet = item.get("snippet", {})
        if not isinstance(snippet, dict):
            snippet = {}
        statistics = item.get("statistics", {})
        if not isinstance(statistics, dict):
            statistics = {}
        title = str(snippet.get("title", "")).strip()
        channel_id = str(snippet.get("channelId", "")).strip()
        channel_title = str(snippet.get("channelTitle", "")).strip()
        view_count = float(statistics.get("viewCount", 0) or 0)
        status = item.get("status", {})
        if not isinstance(status, dict):
            status = {}
        privacy_status = str(status.get("privacyStatus", "")).strip().lower()
        embeddable = bool(status.get("embeddable", True))
        is_public = privacy_status in {"", "public"}
        is_available = bool(is_public and embeddable)

        stats_by_video_id[video_id] = (view_count, title, channel_id, is_available)
        if channel_id:
            if channel_id not in channel_ids:
                channel_ids.append(channel_id)
            channel_title_by_id[channel_id] = channel_title

    channel_subscribers: dict[str, float] = {}
    if channel_ids:
        channel_params = {
            "part": "statistics,snippet",
            "id": ",".join(channel_ids),
            "maxResults": len(channel_ids),
            "key": api_key,
        }
        channel_url = "https://www.googleapis.com/youtube/v3/channels?" + urlencode(channel_params)
        try:
            channel_payload = _http_get_json(channel_url, timeout=timeout)
            for item in channel_payload.get("items", []):
                if not isinstance(item, dict):
                    continue
                channel_id = str(item.get("id", "")).strip()
                if not channel_id:
                    continue
                statistics = item.get("statistics", {})
                if not isinstance(statistics, dict):
                    statistics = {}
                subscribers = float(statistics.get("subscriberCount", 0) or 0)
                channel_subscribers[channel_id] = subscribers
                snippet = item.get("snippet", {})
                if isinstance(snippet, dict):
                    channel_title = str(snippet.get("title", "")).strip()
                    if channel_title:
                        channel_title_by_id[channel_id] = channel_title
        except Exception:
            pass

    artist_tokens = _extract_artist_tokens(owner, artist_credits_json)
    track_core = _strip_feature_text(track_name)

    ranked_candidates: list[dict[str, Any]] = []
    for video_id, (views, title, channel_id, is_available) in stats_by_video_id.items():
        if not is_available:
            continue
        channel_title = channel_title_by_id.get(channel_id, "")
        subscribers = float(channel_subscribers.get(channel_id, 0.0))
        title_overlap = _token_overlap_score(track_core or track_name, title)
        anchor_coverage, strong_anchor = _track_anchor_quality(track_core or track_name, title)
        query_rank = int(query_rank_by_video_id.get(video_id, 99))
        query_boost = max(0.0, 0.8 - 0.2 * float(query_rank))
        official_signal = _official_signal(title)
        channel_match = _channel_match_score(channel_title, artist_tokens)
        title_match = _title_match_score(title, owner, track_core)
        artist_identity = _artist_identity_score(title, channel_title, owner, artist_tokens)
        generic_channel = _is_generic_channel(channel_title)
        lyric_title = _is_lyric_title(title)
        low_trust = (
            artist_identity < 0.75
            and channel_match <= 0.0
            and official_signal <= 0.0
            and anchor_coverage < 0.70
        )
        reject_low_trust = _should_reject_low_trust_candidate(
            views=views,
            channel_match=channel_match,
            official_signal=official_signal,
            artist_identity=artist_identity,
            anchor_coverage=anchor_coverage,
        )
        if reject_low_trust:
            continue

        score = 3.0
        score += official_signal * 2.0
        score += channel_match
        score += title_match
        score += artist_identity * 2.2
        score += title_overlap * 3.2
        score += anchor_coverage * 4.0
        score += _version_alignment_score(track_name, title)
        score += _negative_title_penalty(track_name, title)
        score += _channel_quality_adjustment(channel_title, artist_tokens, views)
        score += query_boost
        score += _live_intent_adjustment(
            request_track=track_name,
            candidate_title=title,
            channel_match=channel_match,
            official_signal=official_signal,
            artist_identity=artist_identity,
            views=views,
        )
        if artist_identity < 0.75:
            score -= 6.0
        if generic_channel and channel_match <= 0.0 and views < 5_000 and official_signal < 2.0:
            score -= 7.0
        if views < 1_000 and channel_match <= 0.0 and official_signal < 2.0 and artist_identity < 1.5:
            score -= 6.0
        if lyric_title and "lyric" not in _normalize_text(track_name):
            score -= 3.0
        if title_overlap < 0.40:
            score -= 4.0
        if anchor_coverage < 0.34 and not strong_anchor:
            score -= 8.0
        if low_trust:
            score -= 6.0
        if track_core and _normalize_text(track_core) in _normalize_text(title):
            score += 1.8
        score += math.log1p(max(0.0, views)) * 0.22
        score += math.log1p(max(0.0, subscribers)) * 0.12

        ranked_candidates.append(
            {
                "video_id": video_id,
                "watch_url": _build_watch_url(video_id),
                "title": title,
                "channel_title": channel_title,
                "views": views,
                "channel_reach_proxy": subscribers,
                "title_overlap": title_overlap,
                "anchor_coverage": anchor_coverage,
                "artist_identity": artist_identity,
                "official_signal": official_signal,
                "generic_channel": generic_channel,
                "lyric_title": lyric_title,
                "low_trust": low_trust,
                "reject_low_trust": reject_low_trust,
                "query_rank": query_rank,
                "score": score,
            }
        )

    if not ranked_candidates:
        fallback = _resolve_from_youtube_search_page(owner, track_name, artist_credits_json, timeout=timeout)
        if not fallback.get("watch_url"):
            fallback["reason"] = "no available API candidates"
        return _cache_and_return(fallback)

    ranked_candidates.sort(
        key=lambda row: (
            float(row.get("score", 0.0)),
            float(row.get("official_signal", 0.0)),
            float(row.get("title_overlap", 0.0)),
            float(row.get("views", 0.0)),
            float(row.get("channel_reach_proxy", 0.0)),
        ),
        reverse=True,
    )

    best = ranked_candidates[0]
    if bool(best.get("low_trust", False)):
        trusted = [row for row in ranked_candidates if not bool(row.get("low_trust", False))]
        if trusted and float(trusted[0].get("score", 0.0)) >= float(best.get("score", 0.0)) - 3.0:
            best = trusted[0]
    best_score = float(best.get("score", 0.0))
    confidence = _confidence_label(best_score, best)

    result = {
        "watch_url": str(best.get("watch_url", "")),
        "video_id": str(best.get("video_id", "")),
        "confidence": confidence,
        "score": best_score,
        "reason": (
            f"{best.get('title', '')} | {best.get('channel_title', '')} | "
            f"views={int(float(best.get('views', 0.0))):,}"
        ),
        "candidates": ranked_candidates,
        "cache_hit": False,
        "api_source": "youtube_api",
    }
    return _cache_and_return(result)
