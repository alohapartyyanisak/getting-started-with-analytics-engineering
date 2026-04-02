# Soft Launch Product Analytics Schema

This doc defines the minimum product analytics layer for the weekend soft launch and early public go-live.

The goal is to measure product adoption and behavior without adding login friction or over-collecting personal data.

## Principles

- keep the launch flow anonymous
- do not use IP address as the primary product identity
- do not require authentication for soft launch
- separate product analytics from server observability
- keep the first event set small and useful

## Identity Model

Use three levels of identifiers:

1. `anonymous_browser_id`
- target end-state is a stable browser-level identifier
- for the weekend launch it currently falls back to Streamlit session-backed state
- used immediately for launch analytics, with retention treated as directional until client-side storage is added

2. `session_id`
- one session/visit
- created on first page load and rotated after inactivity or a new browser session

3. `request_id`
- one backend request
- already useful for joining product events with server logs

Operational note:

- IP address may still be used for abuse/rate limiting and coarse operational analysis
- IP address should not be used as the primary PM metric identity

## Launch Stages

### Soft Launch

Track:

- anonymous browser behavior
- session behavior
- activation funnel
- playback clicks

Do not add:

- login
- saved-user accounts
- email capture unless there is a specific product need

### Public Go-Live

Keep the same anonymous model and add:

- daily aggregate reporting
- cohort tagging such as `soft_launch` vs `public_launch`
- referral/source if available

### Later

Only add authenticated identity if the product needs:

- saved playlists
- persistent preferences
- cross-device continuity
- lifecycle messaging
- true user-level retention

## Minimum Event Envelope

Every product event should include:

- `event_name`
- `event_ts_utc`
- `anonymous_browser_id`
- `session_id`
- `request_id`
- `app_release_version`
- `dataset_snapshot_id`
- `launch_stage`
- `page`
- `event_props`

Recommended `launch_stage` values:

- `soft_launch`
- `public_launch`
- `post_launch`

Recommended `page` values:

- `studio_home`
- `studio_playlist`
- `dev_home`

For launch, the focus should be on `studio`.

## Required Event Set

### Acquisition / Entry

1. `session_started`
- emitted when a new session is established
- props:
  - `referrer`
  - `landing_path`
  - `user_agent_family` if available

2. `landing_viewed`
- emitted when the initial page shell becomes ready
- props:
  - `page_load_ms`

### Input / Selection

3. `mode_selected`
- when the user picks the recommendation mode
- props:
  - `mode`

4. `artist_selected`
- when an artist is added
- props:
  - `artist_name`
  - `selection_count_after`

5. `song_selected`
- when a song is added
- props:
  - `track_name`
  - `artist_name`
  - `selection_count_after`

6. `controls_changed`
- emitted when recommendation sliders or vibe controls change
- props:
  - `control_name`
  - `control_value`

### Activation / Output

7. `playlist_generated`
- emitted when a usable playlist is produced
- props:
  - `track_count`
  - `playlist_minutes_target`
  - `playlist_minutes_actual`
  - `platform_bias`
  - `discovery_mode`
  - `selected_artists_count`
  - `selected_songs_count`
  - `time_to_playlist_ms`

8. `playlist_generation_failed`
- emitted when generation fails or yields no usable result
- props:
  - `failure_reason`

### Engagement / Playback

9. `track_play_clicked`
- emitted when a user clicks a track-level play/open control
- props:
  - `track_name`
  - `artist_name`
  - `platform`
  - `playlist_position`

10. `spotify_opened`
- emitted when a Spotify destination is opened
- props:
  - `track_name`
  - `playlist_position`

11. `youtube_opened`
- emitted when a YouTube destination is opened
- props:
  - `track_name`
  - `playlist_position`

12. `temp_playlist_opened`
- emitted when the generated temporary YouTube playlist link is opened
- props:
  - `playlist_track_count`
  - `coverage_ratio`

## Derived Product Metrics

From the event set above, calculate:

### Acquisition

- sessions per day
- unique browsers per day
- peak day
- peak hour

### Activation

- session-to-playlist-generation rate
- median time to first playlist
- first-play rate
- temp-playlist open rate

### Engagement

- playlists per session
- play clicks per session
- Spotify vs YouTube open split
- average selections before first playlist

### Retention

- repeat browser rate
- D1 browser return rate
- D7 browser return rate

### Quality Proxies

- playlist generation success rate
- abandonment before first play
- no-result rate

## Storage Recommendation For Launch

For launch speed, store product events in structured logs first.

Recommended first path:

- emit product analytics as structured JSON events
- keep them separate from infra/ops events by `event_type`
- aggregate them into daily summaries later

This is faster than standing up a separate analytics platform before the weekend launch.

## Privacy / Retention Guidance

- avoid storing raw IP as a product analytics dimension
- do not store email or personal profile fields for launch unless explicitly needed
- keep anonymous browser identifiers opaque and random
- document retention once long-term analytics storage is added

## Success Criteria For Launch Analytics

The analytics layer is good enough for soft launch when:

- every session has a `session_started`
- playlist success/failure can be counted reliably
- playback intent can be counted reliably
- unique browsers can be estimated without login
- daily acquisition, activation, and engagement summaries can be produced
