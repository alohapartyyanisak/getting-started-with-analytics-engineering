# Soft Launch Product Metrics Dashboard Plan

This doc turns the launch analytics schema into a minimal product dashboard and review rhythm.

The dashboard is for product learning, not infrastructure health.

## Goal

Measure:

- who arrives
- who gets to first value
- who engages with playback
- who comes back

without requiring login before launch.

## Identity Strategy By Stage

### Soft Launch

- anonymous browser id
- session id
- no required auth

### Public Go-Live

- same anonymous model
- add cohort labeling and daily aggregation

### Later

- add authenticated user id only if product value justifies it

## Dashboard A: Launch Product Funnel

Primary audience:

- founder / PM / operator

Update cadence:

- daily during soft launch
- then at least weekly after public go-live

## Recommended Panels

### 1. Sessions Per Day

Answers:

- how many visits are we getting

Metric:

- count of `session_started`

### 2. Unique Browsers Per Day

Answers:

- are we reaching more people or just getting repeat traffic

Metric:

- distinct `anonymous_browser_id` by day

### 3. Playlist Activation Rate

Answers:

- what share of sessions reach first value

Metric:

- sessions with `playlist_generated` / sessions with `session_started`

### 4. Time To First Playlist

Answers:

- how quickly users get value

Metric:

- median and p90 `time_to_playlist_ms`

### 5. First Playback Rate

Answers:

- do users actually try to play something

Metric:

- sessions with any of:
  - `track_play_clicked`
  - `spotify_opened`
  - `youtube_opened`
  - `temp_playlist_opened`

### 6. Platform Preference Split

Answers:

- how users prefer to play

Metric:

- count of `spotify_opened`
- count of `youtube_opened`
- count of `temp_playlist_opened`

### 7. Playlists Per Session

Answers:

- are users exploring multiple recommendation passes

Metric:

- average `playlist_generated` events per session

### 8. Control Interaction Rate

Answers:

- whether users are tuning the engine or accepting defaults

Metric:

- sessions with `controls_changed`
- average control changes per session

### 9. Repeat Browser Rate

Answers:

- are people coming back at all

Metric:

- distinct `anonymous_browser_id` with 2+ sessions in window / total distinct browsers

### 10. D1 / D7 Return Rate

Answers:

- early retention signal without auth

Metric:

- browser returns on day 1 / day 7 after first seen

### 11. No-Result / Failure Rate

Answers:

- how often product flow breaks before value

Metric:

- `playlist_generation_failed`
- sessions with no `playlist_generated`

### 12. Peak Hour / Peak Day

Answers:

- when usage concentrates

Metric:

- session counts by weekday
- session counts by hour

## Soft Launch Review Questions

Ask these every day:

1. Are sessions increasing?
2. Is playlist activation high enough?
3. Is time to first playlist staying low?
4. Are users clicking into playback?
5. Are users returning?
6. Are there hours or days where product usage spikes?
7. Are failures clustering by release or dataset?

## Suggested Success Benchmarks For Soft Launch

These are directional, not hard launch gates.

- activation:
  - at least 50% of sessions generate a playlist
- first playback:
  - at least 25% of sessions click into playback
- time to first playlist:
  - median under 20 seconds
- repeat browser rate:
  - non-zero and trackable within the first week

Adjust later with real usage.

## Storage / Reporting Plan

### Before Launch

- emit product events into structured logs
- capture anonymous browser id and session id

### During Soft Launch

- produce a simple daily summary artifact or query-based report
- keep one daily launch metrics snapshot

### Public Go-Live

- keep the same dashboard
- add:
  - `soft_launch` vs `public_launch` cohort breakdown
  - source/referrer if available

## What Not To Do For Launch

- do not add mandatory login only for analytics
- do not use IP address as the primary product identity
- do not build a heavy analytics stack before the first users arrive

## Launch Limitation

For the weekend launch, `anonymous_browser_id` is currently session-backed rather than cookie/local-storage-backed.

That means:

- launch-week acquisition and activation metrics are still useful
- repeat browser and retention metrics are directional, not fully durable, until the client-side identity helper is added

## Recommended Next Build Step

Implement a minimal product event layer on `studio` for:

- `session_started`
- `landing_viewed`
- `mode_selected`
- `artist_selected`
- `song_selected`
- `controls_changed`
- `playlist_generated`
- `playlist_generation_failed`
- `track_play_clicked`
- `spotify_opened`
- `youtube_opened`
- `temp_playlist_opened`

Then aggregate daily launch metrics from those events.
