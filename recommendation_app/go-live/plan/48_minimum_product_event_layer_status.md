# Minimum Product Event Layer Status

This note records the first launch-ready product event layer added to `studio`.

## Scope

Implemented on:

- `recommendation_app/go-live/code/prod/application_phase2_lighter_UI.py`
- `recommendation_app/go-live/code/prod/go_live_structured_logging.py`

The first-pass event layer emits product analytics into structured logs with:

- `event_type = "product_analytics"`

## Implemented Events

- `session_started`
- `landing_viewed`
- `mode_selected`
- `artist_selected`
- `song_selected`
- `controls_changed`
- `playlist_generated`
- `playlist_generation_failed`

## Current Identity Model

For launch speed, the app currently emits:

- `anonymous_browser_id`
- `session_id`
- `request_id`

Important limitation:

- `anonymous_browser_id` is currently session-backed in Streamlit state
- it is good enough for launch-week session and activation analysis
- it is not yet a durable browser identity across browser restarts or cleared sessions

That means:

- acquisition and activation metrics are usable immediately
- retention metrics are only directional until a client-side storage helper is added

## Why This Is Acceptable For Launch

- no login friction is added
- no user-visible UX changes are required
- the event set is small and stable
- the logs can feed daily product summaries later

## Next Improvement After Launch

Add a client-side storage helper for a more durable:

- `anonymous_browser_id`

That will improve:

- repeat browser rate
- D1 return rate
- D7 return rate
