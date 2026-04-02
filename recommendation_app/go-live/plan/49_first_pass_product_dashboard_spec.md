# First-Pass Product Dashboard Spec

This spec defines the first launch product dashboard built from the minimum `studio` product event layer.

Primary audience:

- founder / PM
- launch operator

Tool target:

- first pass: query-based report on structured product events
- next pass: BigQuery + Looker Studio

## Dashboard Goal

Answer:

- are users arriving
- are they getting to first playlist
- are they engaging with the output
- are failures blocking first value

## Panels

### 1. Sessions

- metric: `session_started`
- view: count by hour and day

### 2. Playlist Activation

- metric: sessions with `playlist_generated` / sessions with `session_started`
- view: daily percentage

### 3. Time To First Playlist

- metric: median and p90 `time_to_playlist_ms`
- source: `playlist_generated`

### 4. Control Interaction

- metric: sessions with any `controls_changed`
- view: count and rate

### 5. Artist and Song Selection Activity

- metric: count of `artist_selected`
- metric: count of `song_selected`
- view: daily totals and per-session averages

### 6. Playlist Failures

- metric: `playlist_generation_failed`
- view: count by day and failure reason

### 7. Mode Mix

- metric: `mode_selected`
- view: count by mode

## Recommended First Review Slice

Use:

- launch day hour-by-hour
- daily totals
- failure reasons grouped by app release version and dataset snapshot id

## Operator Notes

- do not compare raw request volume to product sessions directly
- treat product events as product behavior
- treat Cloud Monitoring as the source of truth for runtime reliability

## Success Read For Launch Day

Good first-day signal:

- sessions are non-zero and increasing
- activation is healthy enough to generate learnings
- failures are explainable and not dominant
- time to first playlist stays within the planned soft-launch target range
