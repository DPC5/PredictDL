# PredictDL

A Flask webserver for browsing player, hero, match, and live-game data, built around a custom **Performance Rating (PR)** system that blends a player's Valve matchmaking rank with their real in-game statistics.

## Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Routes](#routes)
- [Caching](#caching)
- [The Performance Rating (PR) System](#the-performance-rating-pr-system)
  - [1. Rank → Base PR](#1-rank--base-pr)
  - [2. Turning Raw Stats into Z-Scores](#2-turning-raw-stats-into-z-scores)
  - [3. Z-Score → Performance Multiplier (tanh curve)](#3-z-score--performance-multiplier-tanh-curve)
  - [4. Blending Rank and Performance](#4-blending-rank-and-performance)
  - [5. Recency and Confidence Weighting](#5-recency-and-confidence-weighting)
  - [6. Aggregating Across Heroes → Overall PR](#6-aggregating-across-heroes--overall-pr)
  - [7. Percentiles via the Normal CDF](#7-percentiles-via-the-normal-cdf)
  - [Worked Example](#worked-example)
- [Rank Badge Math](#rank-badge-math)
- [Live Match Predictions](#live-match-predictions)
- [Configuration](#configuration)

---

## Overview

The app fetches player and match data from the Deadlock API and Steam Web API, caches it to disk, and renders it through Flask/Jinja templates: player profiles, match breakdowns with death-location heatmaps, a hero gallery, a leaderboard, and a live-match "predictions" board with win-probability estimates.

The centerpiece of the analytics layer is `calcPr()` in `api.py`, which converts a player's raw per-hero statistics into a single 0–~19,000-scale **PR** number, similar in spirit to a chess Elo or an MMR score, but derived from box-score performance rather than win/loss outcomes alone.

## Architecture

```
server.py   Flask app: routes, request handling, template context building,
            disk-cache read/write, rank-badge/image resolution
api.py      Async HTTP layer (aiohttp / curl_cffi) against the Deadlock API
            and Steam Web API, plus all PR/rank math (calcPr, resolve_valve_rank,
            calculate_rank_base_pr)
templates/  Jinja2 templates (../templates relative to server.py)
static/     CSS/JS/image assets (../static)
data/
  config.json          Steam API key + settings
  heros.json            Static hero metadata (id → name/icon mapping)
  player_data/           Per-player JSON cache (PR results, hero stats, rank)
  match_data/             Per-match JSON cache
```

Flask is synchronous, but the data layer is async (`aiohttp`/`asyncio`), so route handlers spin up a short-lived event loop (`asyncio.run(...)` or `asyncio.new_event_loop()` + `run_until_complete`) around each async API call rather than running the whole app under an ASGI server.

## Routes

| Route | Purpose |
|---|---|
| `GET /` | Landing page |
| `GET /search`, `GET /api/search-players` | Player search (by name or Steam ID) |
| `GET /player/<query>` | Player profile: PR, rank widget, hero mastery, tags, match history |
| `GET /player/<query>/refresh` | Force a re-fetch/recompute for a player, bypassing cache |
| `GET /match/<match_id>` | Match detail: per-player stats, item builds, death-location map |
| `GET /heroes`, `GET /heroes/<hero_name>` | Hero gallery / hero detail |
| `GET /leaderboard` | Top players ranked by PR |
| `GET /predictions` | Live matches with win-probability and soul-share estimates |
| `GET /api/active-matches`, `GET /api/active-match/<id>` | JSON polling endpoints backing the predictions page |
| `GET /api/random-hero`, `GET /api/metrics`, `GET /changelog` | Misc/utility endpoints |

## Caching

- **Player cache** (`data/player_data/<steamid3>.json`): stores the last-computed PR breakdown, rank, and match history so profile pages don't re-hit the API on every view. `is_cache_stale()` decides when to refresh.
- **Match cache** (`data/match_data/<match_id>.json`): match detail is immutable once a match has ended, so it's cached indefinitely once fetched (`get_or_fetch_match`).
- **Rank tier asset cache** (`RANK_TIER_ASSETS_CACHE`, in-process): badge artwork URLs fetched from the live `/v1/assets/ranks` endpoint, refreshed via `warm_rank_tier_assets_cache()` at startup. Falls back to `api.py`'s own on-disk cache, and finally to a single bundled placeholder image if both are unavailable.
- **Search popularity** (`increment_and_get_search_count`): a simple counter per `steam64` used to surface "top searched players."

---

## The Performance Rating (PR) System

`calcPr()` produces a rating that says, roughly: *"given this player's rank, are their actual stats (kills, deaths, damage, etc.) better or worse than what someone at that rank typically puts up — and by how much?"*

It runs per-hero (so a player gets a PR for each hero they've played), then aggregates those into one **overall PR** for the account.

### 1. Rank → Base PR

Valve's matchmaking rank (division 1–11 × subrank 1–6) is first collapsed into a single **badge number** from 1 to 66 (`resolve_valve_rank`):

```
badge = (division - 1) * 6 + subrank
```

That badge number is then mapped onto a PR baseline using an **exponential curve** so that separation between ranks grows as rank increases (the gap between two top-rank players is bigger, in PR terms, than the gap between two low-rank players):

```
badge_norm = (badge - 1) / 65                     # normalize to 0.0 – 1.0
rank_base_pr = 1000 + 10500 * badge_norm ^ 1.35
```

- Unranked players default to a flat **1200** base PR.
- Lowest ranked player (badge 1, "Initiate I"): `rank_base_pr ≈ 1000`.
- Highest possible (badge 66, "Eternus VI"): `rank_base_pr = 1000 + 10500 = 11500`.

The exponent **1.35 > 1** is what creates the "distinct separation at high ranks" the code comments describe: for small `badge_norm`, `badge_norm^1.35` is smaller than `badge_norm` itself, compressing the low end; for `badge_norm` near 1 the curve steepens, stretching out the high end.

### 2. Turning Raw Stats into Z-Scores

For each hero, six per-match stats are computed: kills, deaths, assists, hero damage, objective damage, and net worth. Each is converted into a **Z-score** — how many standard deviations above or below the global average a player's stat is — via `calculate_stat_z_score`:

```
z = (value - global_avg) / global_std
```

using live global averages/standard deviations pulled from the Deadlock API's `/analytics/player-stats/metrics` endpoint (`get_global_player_metrics`). Deaths are **inverted** (`is_inverted=True`) before this, since fewer deaths is better:

```
z_deaths = -(deaths - avg_deaths) / std_deaths
```

If global metrics are unavailable, a fallback piecewise-linear mapping is used instead, based on hardcoded "poor" and "great" reference values (`REFS`), scaled onto roughly a −2 to +2 range:

```
norm = (value - poor) / (great - poor)             # clamped to [0, 1]
z ≈ (norm - 0.5) * 4
```

Each stat's Z-score is then combined into one **weighted average Z-score per hero**, using fixed importance weights (deaths weighted highest at 2.0, assists lowest at 1.2):

```
overall_z = Σ(z_i * weight_i) / Σ(weight_i)
```

Stats with a value of zero (e.g. no objective damage recorded) have their weight zeroed out so they don't drag the average down artificially.

### 3. Z-Score → Performance Multiplier (tanh curve)

The combined Z-score is converted into a multiplier applied to the rank's base PR, using a **hyperbolic tangent (tanh)** curve so extreme outliers get diminishing returns instead of blowing up linearly:

```
curved_offset = tanh(PERF_SENSITIVITY * z * 0.5)     # PERF_SENSITIVITY = 1.20

if curved_offset >= 0:
    mult = 1 + curved_offset * (PERF_WEIGHT_MAX - 1)    # ceiling = 1.60
else:
    mult = 1 + curved_offset * (1 - PERF_WEIGHT_MIN)    # floor  = 0.50
```

- `z = 0` (perfectly average stats) → `mult = 1.0`.
- Very high `z` asymptotically approaches `mult → 1.60` (a 60% PR boost, never more).
- Very low `z` asymptotically approaches `mult → 0.50` (halves the rank-driven expectation, never less).

`tanh` is the natural choice here because it's bounded (±1), so no matter how extreme a stat line is, the multiplier can never run away to infinity — it saturates.

### 4. Blending Rank and Performance

The hero's PR blends the rank-only baseline with a performance-adjusted value:

```
performance_driven_pr = rank_base_pr * perf_mult
hero_pr = (rank_base_pr * RANK_BLEND_WEIGHT + performance_driven_pr * (1 - RANK_BLEND_WEIGHT)) * badge_boost
```

With `RANK_BLEND_WEIGHT = 0.40`, this is a **60/40 weighted average** in favor of stats: 40% "who you are according to Valve," 60% "what your numbers actually say," where the stat side is itself scaled off the rank baseline (so a bad game at a high rank still outweighs a great game at a very low rank).

A small extra **badge boost** rewards being a higher rank independent of the blend:

```
badge_boost = 1 + badge_num / 1000        # up to +6.6% at badge 66
```

### 5. Recency and Confidence Weighting

Each hero's contribution to the account-level PR is weighted by two independent factors, both in `[0, 1]`:

**Recency** — an exponential decay with a 90-day half-life, so stats from a match played today count almost fully, while stats from months ago fade out:

```
age_seconds = now - last_played_timestamp
recency_weight = 0.5 ^ (age_seconds / (90 * 86400))
```

At exactly 90 days old, `recency_weight = 0.5`; at 180 days, `0.25`, and so on — classic half-life decay.

**Confidence** — more matches on a hero means more trustworthy stats, using a diminishing-returns curve with `CONFIDENCE_K = 10`:

```
match_confidence = matches / (matches + 10)
```

- 1 match → `0.09` confidence
- 10 matches → `0.50` confidence
- 100 matches → `0.91` confidence

This is the same functional form used in Bayesian-average "shrinkage" formulas — it asymptotically approaches 1 but never quite reaches it, so no amount of matches ever gives a hero literally 100% of the weight.

The final per-hero weight is simply the product:

```
weight = match_confidence * recency_weight
```

### 6. Aggregating Across Heroes → Overall PR

The account's overall PR is a **weight-normalized average** of every hero's PR:

```
overall_pr = Σ(hero_pr_i * weight_i) / Σ(weight_i)
```

i.e. heroes played more recently and more often pull the number harder than heroes played once, six months ago. If a player has no qualifying hero data at all, `overall_pr` falls back to `rank_base_pr`.

### 7. Percentiles via the Normal CDF

Alongside the raw Z-scores, the code derives human-readable **percentiles** ("you perform better than X% of players in this stat") by feeding the Z-score through the standard normal cumulative distribution function, using the Gauss error function `erf`:

```
percentile = 100 * 0.5 * (1 + erf(z / √2))
```

This is the textbook formula for converting a Z-score into a percentile under a normal distribution, clamped to `[0.1, 99.9]` so it never claims literal 0% or 100%. The same Z-scores computed for the PR blend are reused here, so percentiles are effectively "free" — no extra API calls or global data needed.

### Worked Example

A player at **badge 40** (~Ascendant tier) with an overall hero Z-score of **+1.5** (well above average stats):

```
badge_norm      = 39 / 65               = 0.600
rank_base_pr    = 1000 + 10500*0.600^1.35 ≈ 1000 + 10500*0.522 ≈ 6481

curved_offset   = tanh(1.20 * 1.5 * 0.5) = tanh(0.90)          ≈ 0.716
perf_mult       = 1 + 0.716 * (1.60-1.0)                       ≈ 1.430

performance_pr  = 6481 * 1.430                                 ≈ 9268
badge_boost     = 1 + 40/1000                                  = 1.040

hero_pr = (6481*0.40 + 9268*0.60) * 1.040
        = (2592 + 5561) * 1.040
        ≈ 8479
```

So a strong performance (+1.5 Z) at that rank pushes the hero's PR from a ~6,480 rank baseline up to ~8,480 — roughly a 31% boost, capped well short of the theoretical maximum (rank_base_pr × 1.60 × badge_boost) thanks to the `tanh` saturation.

---

## Rank Badge Math

Separately from PR, `server.py` maps a raw badge number (1–66) back to a tier name and subrank for display:

```python
division = (badge_num - 1) // 6 + 1
subrank  = (badge_num - 1) % 6 + 1
```

with 12 tiers (`Obscurus` through `Eternus`), each spanning 6 subranks (divisions I–VI in-game). A player's progress *within* their current subrank is derived from Valve's flat, ever-increasing progress counter, taken **mod 1000** (each subrank spans 1000 progress points):

```python
rp = last_match["player_rank_final_flat_progress"] % 1000
```

Since the Matchmaking Update (2026-07-30), Valve stopped shipping per-subrank badge art, so badge images are resolved live from `/v1/assets/ranks` (one image per 12 tiers) rather than per-subrank local files; the old per-subrank assets are kept only as a last-resort fallback.

## Live Match Predictions

The `/predictions` route reshapes live match data into per-team rows with:

- **Average team MMR/PR**, computed from cached per-player PR data (falling back to `PR * 0.85` as an MMR estimate when no MMR is cached).
- **Soul share**: `team_net_worth / total_net_worth * 100`, i.e. each team's percentage of total in-game net worth ("soul").
- **Momentum bar**: a centered tug-of-war indicator. The offset from the 50/50 midpoint (`|team0_soul_pct - 50|`) sets how far the bar is pushed toward whichever team is leading.
- **Confidence**: the higher of the two teams' win-prediction percentages returned by the API.

## Configuration

All PR tuning constants live in `DEFAULT_CONFIG` inside `calcPr()` and can be overridden per-call via `custom_config`:

| Key | Default | Meaning |
|---|---|---|
| `RANK_BLEND_WEIGHT` | 0.40 | Weight given to rank vs. stat performance |
| `PERF_WEIGHT_MIN` | 0.50 | Floor multiplier for very poor stats |
| `PERF_WEIGHT_MAX` | 1.60 | Ceiling multiplier for elite stats |
| `PERF_SENSITIVITY` | 1.20 | How sharply the tanh curve responds to Z-score |
| `CONFIDENCE_K` | 10.0 | Matches-played needed to reach ~50% confidence |
| `RECENCY_HALF_DAYS` | 90.0 | Half-life (days) for recency decay |

Steam API access is configured via `data/config.json` (`STEAM_API_KEY`), used both for resolving vanity URLs to Steam IDs and for batch-fetching player profile summaries (usernames/avatars) for live-match player lists.