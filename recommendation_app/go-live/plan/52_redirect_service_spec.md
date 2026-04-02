# Redirect Service Spec

This spec defines a lightweight redirect service for outbound product links that need reliable tracking and reliable external navigation.

Recommended deployment target:

- separate Cloud Run service
- small FastAPI app

Recommended service name:

- `dj-mixing-station-redirector`

## Goal

Replace fragile Streamlit-based redirect wrappers with a normal HTTP redirect service that:

1. logs the outbound product event
2. validates the target
3. returns `302` or `303` immediately

## Endpoints

### 1. `GET /healthz`

Purpose:

- health check

Response:

- `200 OK`
- JSON body:
  - `status`
  - `service`

### 2. `GET /open/spotify`

Purpose:

- track and redirect a track-level Spotify open

Emits:

- `track_play_clicked`
- `spotify_opened`

### 3. `GET /open/youtube`

Purpose:

- track and redirect a track-level YouTube open

Emits:

- `track_play_clicked`
- `youtube_opened`

### 4. `GET /open/temp-playlist`

Purpose:

- track and redirect a temporary YouTube playlist open

Emits:

- `temp_playlist_opened`

## Query Params

### Common required params

- `target_url`
- `anonymous_browser_id`
- `session_id`
- `request_id`
- `dataset_snapshot_id`

### Common optional params

- `traffic_source`
- `launch_stage`
- `app_release_version`
- `page`
- `lane`

### Track-level params

Used by `/open/spotify` and `/open/youtube`:

- `track_name`
- `artist_name`
- `playlist_position`

### Temp-playlist params

Used by `/open/temp-playlist`:

- `playlist_track_count`
- `coverage_ratio`

## Validation Rules

Allow only expected targets:

### `/open/spotify`

- `open.spotify.com`

### `/open/youtube`

- `youtube.com`
- `www.youtube.com`
- `music.youtube.com`
- `youtu.be`

### `/open/temp-playlist`

- `youtube.com`
- `www.youtube.com`

Reject:

- missing target
- malformed target
- non-HTTPS target
- host not on the allowlist for the endpoint

Return:

- `400` for bad input
- `403` for disallowed host

## Logging Schema

Event family:

- `event_type = "product_analytics"`

Base envelope:

- `event_name`
- `event_ts`
- `event_ts_utc`
- `anonymous_browser_id`
- `session_id`
- `request_id`
- `app_release_version`
- `dataset_snapshot_id`
- `launch_stage`
- `page`
- `traffic_source`
- `lane`
- `metadata.service`
- `metadata.revision`
- `metadata.configuration`
- `metadata.environment`

### `/open/spotify`

Emit:

1. `track_play_clicked`
- props:
  - `platform = spotify`
  - `track_name`
  - `artist_name`
  - `playlist_position`

2. `spotify_opened`
- props:
  - `track_name`
  - `playlist_position`
  - `target_url`

### `/open/youtube`

Emit:

1. `track_play_clicked`
- props:
  - `platform = youtube`
  - `track_name`
  - `artist_name`
  - `playlist_position`

2. `youtube_opened`
- props:
  - `track_name`
  - `playlist_position`
  - `target_url`

### `/open/temp-playlist`

Emit:

- `temp_playlist_opened`
- props:
  - `playlist_track_count`
  - `coverage_ratio`
  - `target_url`

## Redirect Behavior

Use:

- `303 See Other`

Why:

- clear browser semantics for outbound navigation
- avoids depending on JS execution

## Why This Is Better Than Streamlit Redirect Wrappers

- normal server redirect
- no iframe/component navigation issues
- cleaner browser behavior on mobile
- simpler to validate and debug

## Launch Recommendation

For this weekend launch:

- keep temp playlist as a direct link

After launch:

1. deploy redirector service
2. move Spotify / YouTube / temp playlist outbound opens to redirect endpoints
3. verify logs
4. then rely on those events in product dashboards
