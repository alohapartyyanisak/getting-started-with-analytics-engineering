# DJ Mixing Station Studio  
## AI Product Case Study

---

## Overview

DJ Mixing Station Studio is an offline-first music recommendation product designed to explore a simple question:

**What makes a recommendation feel right, not just relevant?**

The project focuses on building a playlist generation experience that is:
- fast to use
- transparent in behavior
- responsive to user input
- usable in real listening situations

Instead of treating recommendations as a pure ranking problem, this project approaches it as a product problem shaped by user intent, control, and tradeoffs.

---

## Problem

Most recommendation systems are optimized for relevance, but that does not always translate into a good experience.

In practice, users often run into issues like:
- playlists that feel repetitive  
- recommendations that feel random  
- outputs that do not match mood or context  
- results that require manual fixing before use  

A recommendation can be technically correct and still feel off.

The gap is not only about model performance.

It is about **whether the recommendation is usable in the moment it is needed.**

---

## Product Goal

The goal of this project was to design a recommendation experience that:

- helps users get to a playable playlist quickly  
- allows users to shape the output without adding friction  
- makes recommendation behavior more understandable  
- balances familiarity, discovery, and coherence  

Instead of optimizing for a single “best” output, the goal was:

👉 **to create a recommendation experience that feels intentional and usable**

---

## User Need

The product is designed for users who:

- want something to listen to quickly  
- sometimes want familiar content, sometimes discovery  
- want light control over the outcome  
- feel frustrated by black-box recommendations  

Core need:

👉 **“Help me get something I actually want to play right now, without overthinking.”**

---

## Product Design

The experience is built around two main modes:

### Quick Mode
- minimal input  
- vibe-first  
- fast output  

Designed for low-friction usage.

---

### Self Mix Mode
- user selects songs, artists, or a prompt  
- more control over inputs  

Designed for users who want to guide the result.

---

### Core Controls

Users can adjust:
- discovery preference  
- platform flavor  
- playlist duration  
- anchor songs or artists  

These controls are intentionally limited to:
- avoid decision fatigue  
- preserve speed  
- still allow meaningful influence  

---

## Recommendation Approach

The system uses a blended scoring approach that considers:

- similarity to selected anchors  
- discovery preference  
- platform flavor  
- track momentum  

Instead of optimizing a single hidden score, the system balances:

- relevance  
- diversity  
- novelty  
- coherence  

The goal is not perfect ranking.

👉 It is a playlist that **feels right to play**

---

## Key Product Decisions

### 1. Keep control visible, but minimal
Too much control increases friction.  
Too little control reduces trust.

👉 Solution:  
A small set of high-impact controls that shift the output in noticeable ways.

---

### 2. Design around tradeoffs
There is no perfect recommendation.

Key tradeoffs include:
- familiarity vs discovery  
- diversity vs coherence  
- control vs ease  

👉 The focus is on making these tradeoffs feel natural.

---

### 3. Optimize for playability
A recommendation is only useful if users can act on it immediately.

👉 Priority shifted from:
“Is this correct?”  
to  
“Would someone press play on this?”

---

### 4. Use offline version as a product lab
Instead of rushing to deploy:

👉 The offline version is used to:
- test product flow  
- refine ranking behavior  
- validate interaction design  

---

## Evaluation Strategy

Evaluation focuses on product behavior, not just model accuracy.

### Key metrics:
- time to first playlist  
- click-through to Spotify or YouTube  
- fit to target playlist duration  
- amount of user editing before play  

---

### Evaluation principle

👉 **A good recommendation reduces user effort**

---

## What I Learned

### Recommendation is a product problem
The hardest questions were about:
- control  
- trust  
- usability  
- tradeoffs  

Not just modeling.

---

### Accuracy is not enough
A recommendation can be technically correct and still unusable.

---

### User control improves trust
Even small inputs:
- increase engagement  
- make the system feel more understandable  

---

### Evaluation should reflect real usage
Metrics should capture:
- usability  
- friction  
- decision quality  

---

## Future Work

- build a clearer recommendation quality framework  
- test controlled vs automated experiences  
- collect user feedback on playability  
- improve deployment and real-time experience  

---

## Why This Project Matters

This project reflects how I approach data science:

- connecting models to real user behavior  
- thinking in terms of product outcomes  
- designing for usability, not just accuracy  

I am most interested in building systems that:
- help users make decisions  
- feel understandable  
- and create real-world value
