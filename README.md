# DJ Mixing Station Studio

Offline-first recommender system for transparent playlist generation, user-controlled ranking, and explainable music discovery.

## Why this project exists
Many recommendation experiences feel like a black box. I wanted to build a product that helps users quickly get to a playable playlist while still showing them how the ranking behavior changes as they adjust the controls.

## Current status
- Rolled out an offline version for hands-on product validation and iteration
- Go-live version is actively in development based on a longer-term master plan
- Current work includes strengthening deployment readiness, product flow, and the hosted user experience

## What the product does
DJ Mixing Station Studio helps users generate playlists through two main modes:
- **Quick Mode:** start with a vibe-first workflow
- **Self Mix:** build from selected songs, artists, or a surprise-mode prompt

Users can:
- tune discovery preference
- adjust platform flavor
- target playlist duration
- review transparent recommendation behavior
- open outputs in Spotify or YouTube

## Product principles
- **Copilot, not black box:** controls stay visible so users can shape the result
- **Fast time to first playlist:** reduce friction from idea to playable output
- **Explainable ranking:** show why results move when the controls change
- **Iterative product development:** use the offline version to learn before scaling the go-live experience

## Recommendation approach
The recommender uses a blended scoring approach that considers:
- similarity to selected anchors
- discovery preference
- platform flavor
- track momentum

The goal is to balance relevance, exploration, and user control rather than optimize for one hidden score.

## Evaluation mindset
Key product metrics include:
- time to first playlist
- click-through to Spotify or YouTube
- fit to the target duration window
- amount of user editing before play

## Tech stack
- Python
- Streamlit
- Recommendation logic and ranking diagnostics
- Local/offline execution
- GitHub Actions, Docker, and cloud build configuration for the go-live path

## Repository structure
- `recommendation_app/` - core app and ranking workflow
- `.github/workflows/` - automation and checks
- `Dockerfile` - containerization for deployment
- `cloudbuild.*.yaml` - cloud-oriented build flows
con
## Why it matters
This project reflects how I think about recommendation systems as a product scientist: ranking quality, transparent controls, user behavior, measurable outcomes, and iterative shipping.

## Case Study
A deeper look into product decisions, tradeoffs, and evaluation thinking:

- [Read the Case Study](docs/CASE_STUDY.md)

## Quick Links
- [Case Study (PDF)](docs/DJ_Mixing_Station_Studio_Case_Study.pdf)
- [Medium Tech Post](https://medium.com/@yanisakk26/your-taste-your-vibe-how-dj-mixing-station-studio-reimagines-recommendations-tech-8f0472d93b4d)
- [Master Plan / Go-Live Roadmap](docs/masterplan_go-live_roadmap.md)
