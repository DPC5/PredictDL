from flask import Flask, render_template, request, redirect, url_for, jsonify
import asyncio
import aiohttp
import os
import math
from pathlib import Path
import json
import time
import api
from datetime import datetime, timezone
import random

from api import (
    get_latest_patch,
    resolve_steam_id,
    steam64_to_steamid3,
    get_deadlock_hero_stats,
    get_player_match_history,
    enrich_match_with_local_data,
    get_match_metadata,
    get_most_played_heros,
    get_active_matches,
    get_global_player_metrics,
    steamid3_to_steam64,
    get_player_rank,
    get_player_rank_image_url,
    get_rank_tier_assets,
    calcPr,
    get_item_info,
    get_final_items,
    get_steam_profile,
    build_hero_mastery_stats,
    HEROS as API_HEROS_MAP,
)

app = Flask(__name__,
            template_folder='../templates',
            static_folder='../static')

CURRENT_DIR = Path(__file__).parent
BASE_DIR = CURRENT_DIR.parent
CONFIG_FILE = BASE_DIR / 'data' / 'config.json'
PLAYER_DATA_DIR = BASE_DIR / 'data' / 'player_data'
MATCH_DATA_DIR = BASE_DIR / 'data' / 'match_data'
HEROS_FILE = BASE_DIR / 'data' / 'heros.json'

PLAYER_DATA_DIR.mkdir(parents=True, exist_ok=True)
MATCH_DATA_DIR.mkdir(parents=True, exist_ok=True)

with open(CONFIG_FILE, 'r') as file:
    config = json.load(file)
STEAM_API_KEY = config.get('STEAM_API_KEY')

with open(HEROS_FILE, 'r', encoding='utf-8') as f:
    HEROS = json.load(f)

latest_patch_cache = None
CACHE_MAX_AGE_SECONDS = 2 * 24 * 60 * 60

# ── Rank badges (Matchmaking Update) ────────────────────────────────────────
# As of the Matchmaking Update (2026-07-30), the game stopped shipping
# per-subrank badge art -- /v1/assets/ranks now serves ONE image per tier
# (Obscurus, Initiate, ... Eternus), not one per subrank. RANK_NAMES stays a
# flat list of the 12 tiers so existing badge-number math (1-66) keeps working;
# badge art is now resolved through RANK_TIER_ASSETS_CACHE (populated from the
# live API by warm_rank_tier_assets_cache()) instead of local per-subrank files.
RANK_NAMES = [
    "Obscurus", "Initiate", "Seeker", "Alchemist", "Arcanist",
    "Ritualist", "Emissary", "Archon", "Oracle", "Phantom",
    "Ascendant", "Eternus",
]

# Old per-subrank local files. Deprecated by the game/API but kept as a
# last-resort fallback for the rare case the live /v1/assets/ranks fetch
# fails AND no disk cache exists yet (e.g. first run with no network).
LEGACY_RANK_IMAGE_FALLBACK = "/static/images/ranks/Obscurus.webp"

# In-process cache of {tier_name: {"large_webp":..,"large":..,"chalk_webp":..,"chalk":..}}
# populated by warm_rank_tier_assets_cache(). Kept in-process (in addition to
# api.py's on-disk cache) so template rendering doesn't need to be async.
RANK_TIER_ASSETS_CACHE: dict = {}


def warm_rank_tier_assets_cache():
    """Populates RANK_TIER_ASSETS_CACHE from the live API (or its disk cache)."""
    global RANK_TIER_ASSETS_CACHE
    try:
        assets = asyncio.run(get_rank_tier_assets())
        if assets:
            RANK_TIER_ASSETS_CACHE = assets
    except Exception as e:
        print(f"Error warming rank tier assets cache: {e}")


def get_rank_badge_image(tier_name: str, variant: str = "large_webp") -> str:
    """
    Best-effort badge image URL for a tier name ("Obscurus", "Initiate", ...).
    Prefers the live-fetched large/large_webp art; falls back to the old local
    Obscurus placeholder only if the live asset cache is completely empty.
    """
    info = RANK_TIER_ASSETS_CACHE.get(tier_name)
    if info:
        return (
            info.get(variant)
            or info.get("large_webp")
            or info.get("large")
            or info.get("chalk_webp")
            or info.get("chalk")
            or LEGACY_RANK_IMAGE_FALLBACK
        )
    return LEGACY_RANK_IMAGE_FALLBACK


def badge_num_to_tier_and_subrank(badge_num: int) -> tuple[str, int]:
    """badge 1-66 -> ("Initiate", 1) ... ("Eternus", 6). badge <= 0 -> ("Obscurus", 0)."""
    if badge_num <= 0:
        return "Obscurus", 0
    badge_num = max(1, min(66, badge_num))
    division = (badge_num - 1) // 6 + 1
    subrank = (badge_num - 1) % 6 + 1
    if division >= len(RANK_NAMES):
        return "Eternus", subrank
    return RANK_NAMES[division], subrank


def badge_to_image(badge: int) -> str:
    tier_name, _ = badge_num_to_tier_and_subrank(badge)
    return get_rank_badge_image(tier_name)


def mmr_to_badge(mmr_score: float) -> int:
    # Fixed scale to strictly map 100-6600 to badge 1-66
    if mmr_score < 100:
        return 0
    rank_index = int(math.floor((mmr_score - 100) / 100))
    return max(1, min(66, rank_index + 1))


def normalize_rank_image_url(raw: str | None) -> str:
    """
    Turns a stored rank_image value into something an <img src> can use
    directly. Badge art now comes from the live deadlock-api.com CDN
    (absolute https:// URLs) instead of local /static/ files, so this needs
    to leave absolute URLs alone -- only relative legacy paths get the
    /static/ prefix.
    """
    if not raw:
        return LEGACY_RANK_IMAGE_FALLBACK
    if raw.startswith('http://') or raw.startswith('https://') or raw.startswith('/static/'):
        return raw
    return f"/static/{raw}"


def resolve_rank_widget(player_rank: dict | None, steamid3: str | None = None) -> dict:
    """
    Builds everything the player-screen rank widget needs: current badge
    image (fetched live from the /rank/image endpoint -- already has the
    I-VI division numeral baked in), next-rank badge image (plain tier art
    from /v1/assets/ranks, since /rank/image only renders *this* player's
    actual rank), tier/subrank labels, and a progress-points bar.

    `player_rank` is the dict returned by api.get_player_rank():
        {"badge": int, "rank": int, "subrank": int, "last_match": dict|None}

    A subrank spans 1000 progress points. The player's current progress
    within their subrank comes from last_match.player_rank_final_flat_progress
    (the flat, ever-increasing progress counter Valve reports), taken mod
    1000 -- the /rank endpoint itself only reports the resulting badge, not
    a ready-made "X/1000" number, so this is how that number is derived.
    """
    if not RANK_TIER_ASSETS_CACHE:
        # Lazy fallback in case the app entrypoint skipped warm_rank_tier_assets_cache()
        warm_rank_tier_assets_cache()

    # Number of visual segments to split the progress bar into on the frontend
    TOTAL_SEGMENTS = 5

    badge = (player_rank or {}).get("badge") or 0
    rank = (player_rank or {}).get("rank") or 0
    subrank = (player_rank or {}).get("subrank") or 0
    last_match = (player_rank or {}).get("last_match")

    is_unranked = badge <= 0 and rank <= 0

    # Current badge image: pull straight from the live rank/image endpoint
    # when we know who the player is, since it's account-specific (numeral
    # baked in). Fall back to the plain Obscurus tier art otherwise.
    current_badge_image = (
        get_player_rank_image_url(steamid3) if steamid3 else get_rank_badge_image("Obscurus")
    )

    if is_unranked:
        in_placement = last_match is not None
        return {
            "is_unranked": True,
            "in_placement": in_placement,
            "current_badge_image": current_badge_image,
            "current_tier_label": "In Placement" if in_placement else "Obscurus",
            "next_badge_image": None,
            "next_tier_label": None,
            "rp": 0,
            "max_rp": 1000,
            "rp_percent": 0,
            "rp_known": False,
            "segments": [{"fill_percent": 0} for _ in range(TOTAL_SEGMENTS)],
        }

    current_tier_name = RANK_NAMES[rank] if 0 <= rank < len(RANK_NAMES) else "Eternus"
    is_max_rank = rank >= len(RANK_NAMES) - 1 and subrank >= 6

    if is_max_rank:
        next_badge_image = None
        next_tier_label = "MAX RANK"
    else:
        if subrank >= 6:
            next_rank_idx, next_subrank = rank + 1, 1
        else:
            next_rank_idx, next_subrank = rank, subrank + 1
        next_tier_name = RANK_NAMES[next_rank_idx] if next_rank_idx < len(RANK_NAMES) else "Eternus"
        next_badge_image = get_rank_badge_image(next_tier_name)
        next_tier_label = f"{next_tier_name} {next_subrank}" if next_tier_name != "Obscurus" else next_tier_name

    # Progress points within the current subrank (0-1000)
    max_rp = 1000
    rp_known = False
    rp = 0
    if last_match:
        final_progress = last_match.get("player_rank_final_flat_progress")
        if final_progress is not None:
            rp = max(0, min(max_rp, int(final_progress) % max_rp))
            rp_known = True

    # Calculate fill percentage for each segment independently
    segments = []
    rp_per_segment = max_rp / TOTAL_SEGMENTS

    for i in range(TOTAL_SEGMENTS):
        segment_start_rp = i * rp_per_segment
        segment_end_rp = (i + 1) * rp_per_segment

        if rp >= segment_end_rp:
            segments.append({"fill_percent": 100})
        elif rp <= segment_start_rp:
            segments.append({"fill_percent": 0})
        else:
            fill = ((rp - segment_start_rp) / rp_per_segment) * 100
            segments.append({"fill_percent": round(fill, 1)})

    return {
        "is_unranked": False,
        "in_placement": False,
        "current_badge_image": current_badge_image,
        "current_tier_label": f"{current_tier_name} {subrank}" if current_tier_name != "Obscurus" else current_tier_name,
        "next_badge_image": next_badge_image,
        "next_tier_label": next_tier_label,
        "rp": rp,
        "max_rp": max_rp,
        "rp_percent": round((rp / max_rp) * 100, 1) if rp_known else 0,
        "rp_known": rp_known,
        "segments": segments,
    }


# ── Metrics Helper Logic ───────────────────────────────────────────────────

def get_metrics() -> dict:
    total_players = 0
    total_matches = 0
    if PLAYER_DATA_DIR.exists():
        for cache_file in PLAYER_DATA_DIR.glob('*.json'):
            total_players += 1
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    total_matches += data.get('total_matches', 0)
            except Exception as e:
                print(f"Error reading metric cache file {cache_file}: {e}")
                continue
    return {
        "total_players": total_players,
        "total_matches": total_matches
    }


# ── Player data cache helpers ───────────────────────────────────────────────

def _cache_path(steamid3: str) -> Path:
    return PLAYER_DATA_DIR / f"{steamid3}.json"


def load_player_cache(steamid3: str) -> dict | None:
    path = _cache_path(steamid3)
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Failed to read player cache for {steamid3}: {e}")
        return None


def save_player_cache(steamid3: str, data: dict) -> None:
    data['fetched_at'] = time.time()
    path = _cache_path(steamid3)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Failed to write player cache for {steamid3}: {e}")


def is_cache_stale(cache: dict) -> bool:
    fetched_at = cache.get('fetched_at', 0)
    return (time.time() - fetched_at) > CACHE_MAX_AGE_SECONDS


def fetched_at_display(cache: dict) -> str:
    fetched_at = cache.get('fetched_at')
    if not fetched_at:
        return "Unknown"
    dt = datetime.fromtimestamp(fetched_at, tz=timezone.utc)
    return dt.strftime("%B %d, %Y at %H:%M UTC")


def increment_and_get_search_count(steam64: str) -> int:
    counts_file = BASE_DIR / 'data' / 'search_counts.json'
    counts_file.parent.mkdir(parents=True, exist_ok=True)
    
    counts = {}
    if counts_file.exists():
        try:
            with open(counts_file, 'r', encoding='utf-8') as f:
                counts = json.load(f)
        except Exception:
            counts = {}

    current_count = counts.get(steam64, 0) + 1
    counts[steam64] = current_count

    try:
        with open(counts_file, 'w', encoding='utf-8') as f:
            json.dump(counts, f, indent=2)
    except Exception as e:
        print(f"Error updating search_counts.json: {e}")

    return current_count


def get_top_searched_players(limit=3) -> list:
    counts_file = BASE_DIR / 'data' / 'search_counts.json'
    if not counts_file.exists():
        return []

    try:
        with open(counts_file, 'r', encoding='utf-8') as f:
            counts = json.load(f)
    except Exception:
        return []

    sorted_players = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
    
    trending_list = []
    for steam64, count in sorted_players:
        try:
            steamid3 = steam64_to_steamid3(steam64)
        except Exception:
            continue
            
        cache_path = PLAYER_DATA_DIR / f"{steamid3}.json"
        
        if cache_path.exists():
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    profile_data = json.load(f)
                
                most_played_list = profile_data.get("most_played", [])
                top_hero = most_played_list[0][0] if most_played_list else "Unknown Hero"
                
                pr_data = profile_data.get("pr_data", {})
                heroes = pr_data.get("heroes", [])
                
                total_w = sum(h.get("matches_played", 0) for h in heroes)
                total_wins = sum((h.get("win_rate", 0) * h.get("matches_played", 0)) for h in heroes)
                win_rate = int(round(total_wins / total_w * 100)) if total_w > 0 else 0
                
                match_history = profile_data.get("match_history", [])
                total_k = sum(m.get("kills", 0) for m in match_history)
                total_d = sum(m.get("deaths", 0) for m in match_history)
                total_a = sum(m.get("assists", 0) for m in match_history)
                kda = round((total_k + total_a) / total_d, 1) if total_d > 0 else 0.0

                # NOTE: badge art now comes from the live deadlock-api.com CDN
                # (absolute https:// URLs), not local /static/ files -- this is
                # a ready-to-use <img src> value now, not a url_for() filename.
                # If index.html renders this via url_for('static', filename=...),
                # switch it to `<img src="{{ t.rank_image }}">` directly.
                clean_rank = normalize_rank_image_url(profile_data.get("rank_image"))

                trending_list.append({
                    "steam64": steam64,
                    "personaname": profile_data.get("player", {}).get("personaname", f"User #{str(steam64)[-4:]}"),
                    "avatar": profile_data.get("player", {}).get("avatarfull", "/static/images/unknown.png"),
                    "pr": round(profile_data.get("pr_data", {}).get("overall_pr", 0)),
                    "main_hero": top_hero,
                    "views": count,
                    "rank_image": clean_rank,  # Cleaned path for url_for('static', filename=...)
                    "win_rate": win_rate,
                    "kda": kda,
                    "total_matches": profile_data.get("total_matches", 0)
                })
            except Exception as e:
                print(f"Failed to compile trending data for {steam64}: {e}")
                continue
                
    return trending_list


# ── Match Cache Tools ───────────────────────────────────────────────

def _match_cache_path(match_id) -> Path:
    return MATCH_DATA_DIR / f"{match_id}.json"

def load_match_cache(match_id) -> dict | None:
    path = _match_cache_path(match_id)
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        return None

def save_match_cache(match_id, data: dict) -> None:
    path = _match_cache_path(match_id)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception as e:
        pass


def get_or_fetch_match(match_id: int) -> dict | None:
    cached = load_match_cache(match_id)
    if cached:
        return cached

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        data = loop.run_until_complete(get_match_metadata(match_id))
    finally:
        loop.close()

    if data:
        save_match_cache(match_id, data)
    return data


# ── Player Teammates/Laning Logic ───────────────────────────────────────

def analyze_player_relationships(steamid3: str, match_history: list) -> list:
    teammates = {}
    
    for m in match_history:
        match_id = m.get("match_id")
        won = m.get("won", False)
        
        cached = load_match_cache(match_id)
        if not cached:
            continue
            
        match_info = cached.get("match_info") or cached
        players = match_info.get("players") or []
        
        user_team = None
        user_party = None
        for p in players:
            if str(p.get("account_id")) == str(steamid3):
                user_team = p.get("team") or p.get("player_team")
                user_party = p.get("party") 
                break
                
        if user_team is None:
            continue

        for p in players:
            aid = str(p.get("account_id"))
            if aid == str(steamid3) or not aid or aid == "0":
                continue
                
            p_team = p.get("team") or p.get("player_team")
            if p_team == user_team:
                if aid not in teammates:
                    hero_id = p.get("hero_id") or p.get("hero")
                    hero_name = p.get("hero_name")
                    if not hero_name or str(hero_name).lower() in ("unknown", "unknown hero", "none"):
                        if hero_id is not None:
                            try:
                                hero_id_int = int(hero_id)
                                hero_name = API_HEROS_MAP.get(hero_id_int) or API_HEROS_MAP.get(str(hero_id_int))
                            except (ValueError, TypeError):
                                hero_name = None
                    if not hero_name:
                        hero_name = "Unknown"

                    teammates[aid] = {
                        "account_id": steamid3_to_steam64(aid),
                        "hero_name": hero_name,
                        "matches": 0,
                        "wins": 0,
                        "lane_partner": False
                    }
                teammates[aid]["matches"] += 1
                if won:
                    teammates[aid]["wins"] += 1

    sorted_teammates = sorted(teammates.values(), key=lambda x: x["matches"], reverse=True)
    
    for i, tm in enumerate(sorted_teammates):
        if tm["matches"] >= 3 and i < 2:
            tm["lane_partner"] = True
            
    return sorted_teammates[:15]


# ── Mastery Tab Helpers ─────────────────────────────────────────────────


# Rank-letter thresholds for the Mastery tab. Same "house-made flavor stat"
# philosophy as before (see mastery_score above) -- just presented the way a
# competitive-game rank screen would (S/A/B/C/D with +/- gradations) instead
# of the old wordy tier names. `glow` marks the ranks that should render with
# the bloom/glow effect next to the hero name (reserved for the S ranks so it
# actually feels special).
_MASTERY_LETTER_THRESHOLDS = [
    (1100, "S+", True),
    (900,  "S",  True),
    (700,  "A+", False),
    (550,  "A",  False),
    (400,  "B+", False),
    (300,  "B",  False),
    (180,  "C+", False),
    (100,  "C",  False),
    (0,    "D",  False),
]


def _mastery_letter_grade(mastery_score: float) -> tuple:
    for threshold, letter, glow in _MASTERY_LETTER_THRESHOLDS:
        if mastery_score >= threshold:
            return letter, glow
    return "D", False


def build_player_mastery(steamid3: str, match_history: list, most_played: list, top_n: int = 5) -> list:
    """Assembles the Mastery tab payload: one deep-dive card per one of the
    player's top-N most-played heroes."""
    mastery_list = []

    for hero_name, matches_count, hero_id in most_played[:top_n]:
        if matches_count <= 0:
            continue

        base = build_hero_mastery_stats(match_history, hero_id, hero_name)
        if not base:
            continue

        # "Mastery Score" is a fun, transparent house-made flavor stat (volume
        # of games + win rate + longest win streak) -- Deadlock has no
        # official mastery system, so this is clearly our own invention,
        # similar in spirit to the existing custom PR rating on this site.
        mastery_score = round(
            matches_count * 12 + (base["win_rate"] * 4) + (base["longest_win_streak"] * 15)
        )
        base["mastery_score"] = mastery_score
        letter, glow = _mastery_letter_grade(mastery_score)
        base["mastery_tier"] = letter
        base["mastery_glow"] = glow

        mastery_list.append(base)

    return mastery_list


# ── Player Tags Generator ───────────────────────────────────────────────

def generate_player_tags(hero_stats: list, match_history: list, total_matches: int) -> list:
    tags_file = BASE_DIR / 'data' / 'tags.json'
    
    default_config = {
        "steady_combatant": {
            "text": "Steady Combatant",
            "hovertext": "Maintains balanced standard statistical outputs.",
            "color": "bg-zinc-500/10 text-zinc-400 border-zinc-500/30",
            "priority": 0
        },
        "one_trick_pony": {
            "text": "OTP",
            "hovertext": "Plays one hero almost exclusively.",
            "color": "bg-purple-500/10 text-purple-400 border-purple-500/30",
            "priority": 90
        },
        "terrible_hero": {
            "text": "Terrible Main",
            "hovertext": "Maintains a low win rate on their most played hero.",
            "color": "bg-red-500/10 text-red-400 border-red-500/30",
            "priority": 80
        },
        "versatile": {
            "text": "Versatile",
            "hovertext": "Plays a wide variety of heroes.",
            "color": "bg-blue-500/10 text-blue-400 border-blue-500/30",
            "priority": 70
        },
        "kda_warrior": {
            "text": "KDA Warrior",
            "hovertext": "Exceptionally high Kill/Death/Assist ratio.",
            "color": "bg-orange-500/10 text-orange-400 border-orange-500/30",
            "priority": 60
        },
        "bloodthirsty": {
            "text": "Bloodthirsty",
            "hovertext": "Maintains an exceptionally high kill count per match.",
            "color": "bg-red-600/10 text-red-500 border-red-600/30",
            "priority": 68
        },
        "feeder": {
            "text": "Feeder",
            "hovertext": "High average death rate across matches.",
            "color": "bg-red-900/20 text-red-500 border-red-800/50",
            "priority": 85
        },
        "survivor": {
            "text": "Survivor",
            "hovertext": "Rarely dies during matches.",
            "color": "bg-teal-500/10 text-teal-400 border-teal-500/30",
            "priority": 72
        },
        "wingman": {
            "text": "Wingman",
            "hovertext": "High average assists across recent matches.",
            "color": "bg-cyan-500/10 text-cyan-400 border-cyan-500/30",
            "priority": 50
        },
        "soul_harvester": {
            "text": "Soul Harvester",
            "hovertext": "Averages extremely high net worth across matches.",
            "color": "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
            "priority": 65
        },
        "tower_terror": {
            "text": "Tower Terror",
            "hovertext": "Deals massive damage to map objectives.",
            "color": "bg-yellow-500/10 text-yellow-400 border-yellow-500/30",
            "priority": 78
        },
        "objective_focused": {
            "text": "Objective Gamer",
            "hovertext": "Deals strong objective damage.",
            "color": "bg-amber-500/10 text-amber-400 border-amber-500/30",
            "priority": 75
        },
        "early_game_menace": {
            "text": "Early Menace",
            "hovertext": "High kills in fast matches.",
            "color": "bg-rose-500/10 text-rose-400 border-rose-500/30",
            "priority": 55
        },
        "late_game_bloomer": {
            "text": "Late Bloomer",
            "hovertext": "Wins long, drawn-out matches.",
            "color": "bg-indigo-500/10 text-indigo-400 border-indigo-500/30",
            "priority": 55
        }
    }
    
    config = default_config.copy()
    
    if tags_file.exists():
        try:
            with open(tags_file, 'r', encoding='utf-8') as f:
                user_config = json.load(f)
                for k, v in user_config.items():
                    if k in config:
                        config[k].update(v)
                    else:
                        config[k] = v
        except Exception as e:
            print(f"Error reading tags.json: {e}")

    enriched_hero_stats = []
    if hero_stats:
        for h in hero_stats:
            h_copy = dict(h)
            hid = h_copy.get("hero_id")
            if "hero_name" not in h_copy or not h_copy["hero_name"]:
                h_copy["hero_name"] = API_HEROS_MAP.get(hid, "Hero")
            mp = h_copy.get("matches_played", 0)
            if "win_rate" not in h_copy or h_copy["win_rate"] is None:
                h_copy["win_rate"] = (h_copy.get("wins", 0) / mp) if mp > 0 else 0.0
            enriched_hero_stats.append(h_copy)

    player_tags = []

    if enriched_hero_stats and total_matches > 0:
        sorted_by_played = sorted(enriched_hero_stats, key=lambda x: x.get("matches_played", 0), reverse=True)
        main_hero = sorted_by_played[0]
        main_played = main_hero.get("matches_played", 0)
        hero_name = main_hero.get("hero_name", "Hero")
        
        if main_played >= 3:
            if (main_played / total_matches) >= 0.4:
                tag = config.get("one_trick_pony", {}).copy()
                if tag:
                    tag["text"] = f"OTP: {hero_name}"
                    tag["hovertext"] = f"{tag.get('hovertext', '')} (Pick Rate: {round((main_played / total_matches) * 100)}%)"
                    player_tags.append(tag)
            
            main_wr = main_hero.get("win_rate", 1.0)
            if main_wr < 0.38:
                tag = config.get("terrible_hero", {}).copy()
                if tag:
                    tag["text"] = f"Terrible {hero_name}"
                    tag["hovertext"] = f"{tag.get('hovertext', '')} (Win Rate: {round(main_wr * 100)}% over {main_played} matches)"
                    player_tags.append(tag)
                    
        if total_matches >= 10 and len(enriched_hero_stats) >= 4:
            if (main_played / total_matches) <= 0.35:
                tag = config.get("versatile", {}).copy()
                if tag: 
                    tag["hovertext"] = f"{tag.get('hovertext', '')} (Top Hero Pick Rate: {round((main_played / total_matches) * 100)}%)"
                    player_tags.append(tag)

    if match_history:
        total_tracked = len(match_history)
        sum_kills = 0
        sum_deaths = 0
        sum_assists = 0
        sum_obj_dmg = 0
        sum_duration = 0
        sum_nw = 0
        wins = 0

        for m in match_history:
            sum_kills += int(m.get("kills") or m.get("player_kills") or 0)
            sum_deaths += int(m.get("deaths") or m.get("player_deaths") or 0)
            sum_assists += int(m.get("assists") or m.get("player_assists") or 0)
            sum_obj_dmg += int(m.get("objective_damage") or m.get("obj_damage") or 0)
            sum_nw += int(m.get("net_worth") or m.get("networth") or m.get("gold") or 0)
            
            duration_s = m.get("match_duration_s") or m.get("duration_s") or 0
            sum_duration += int(duration_s)
            if m.get("won"):
                wins += 1

        if total_tracked > 0:
            avg_kills = sum_kills / total_tracked
            avg_deaths = sum_deaths / total_tracked
            avg_assists = sum_assists / total_tracked
            avg_obj = sum_obj_dmg / total_tracked
            avg_duration = sum_duration / total_tracked
            avg_nw = sum_nw / total_tracked
            
            kda = (sum_kills + sum_assists) / max(1, sum_deaths)

            if kda >= 3.5 and "kda_warrior" in config:
                tag = config["kda_warrior"].copy()
                tag["hovertext"] = f"{tag.get('hovertext', '')} (Avg KDA: {kda:.1f})"
                player_tags.append(tag)
                
            if avg_kills >= 12 and "bloodthirsty" in config:
                tag = config["bloodthirsty"].copy()
                tag["hovertext"] = f"{tag.get('hovertext', '')} (Avg Kills: {avg_kills:.1f})"
                player_tags.append(tag)
                
            if avg_deaths > 8 and kda < 1.5 and "feeder" in config:
                tag = config["feeder"].copy()
                tag["hovertext"] = f"{tag.get('hovertext', '')} (Avg Deaths: {avg_deaths:.1f})"
                player_tags.append(tag)
                
            if avg_deaths < 3.5 and "survivor" in config:
                tag = config["survivor"].copy()
                tag["hovertext"] = f"{tag.get('hovertext', '')} (Avg Deaths: {avg_deaths:.1f})"
                player_tags.append(tag)
                
            if avg_assists >= 10 and "wingman" in config:
                tag = config["wingman"].copy()
                tag["hovertext"] = f"{tag.get('hovertext', '')} (Avg Assists: {avg_assists:.1f})"
                player_tags.append(tag)
                
            if avg_nw > 40000 and "soul_harvester" in config:
                tag = config["soul_harvester"].copy()
                tag["hovertext"] = f"{tag.get('hovertext', '')} (Avg Net Worth: {int(avg_nw):,})"
                player_tags.append(tag)
                
            if avg_obj > 6000 and "tower_terror" in config:
                tag = config["tower_terror"].copy()
                tag["hovertext"] = f"{tag.get('hovertext', '')} (Avg Obj Dmg: {int(avg_obj):,})"
                player_tags.append(tag)
                
            elif avg_obj > 3000 and "objective_focused" in config:
                tag = config["objective_focused"].copy()
                tag["hovertext"] = f"{tag.get('hovertext', '')} (Avg Obj Dmg: {int(avg_obj):,})"
                player_tags.append(tag)

            if avg_kills >= 6 and avg_duration < 1500 and "early_game_menace" in config:
                tag = config["early_game_menace"].copy()
                tag["hovertext"] = f"{tag.get('hovertext', '')} (Avg Kills: {avg_kills:.1f}, Avg Duration: {int(avg_duration//60)}m)"
                player_tags.append(tag)
            
            if avg_duration > 2400 and (wins / total_tracked) >= 0.5 and "late_game_bloomer" in config:
                tag = config["late_game_bloomer"].copy()
                tag["hovertext"] = f"{tag.get('hovertext', '')} (Win Rate: {round((wins / total_tracked) * 100)}%, Avg Duration: {int(avg_duration//60)}m)"
                player_tags.append(tag)

    if not player_tags and "steady_combatant" in config:
        tag = config["steady_combatant"].copy()
        tag["hovertext"] = f"{tag.get('hovertext', '')} (Matches Analyzed: {len(match_history) if match_history else 0})"
        player_tags.append(tag)

    valid_tags = []
    for t in player_tags:
        if isinstance(t, dict) and t.get("text") and t.get("color"):
            valid_tags.append(t)

    valid_tags.sort(key=lambda x: x.get("priority", 0), reverse=True)
    return valid_tags


def generate_performance_stats(match_history: list) -> dict | None:
    if not match_history:
        return None
    
    total_kills = 0
    total_deaths = 0
    total_assists = 0
    wins = 0
    total_matches = len(match_history)
    total_damage = 0
    total_obj_damage = 0
    
    net_wins = 0
    net_wins_history = []
    
    for m in reversed(match_history):
        k = int(m.get("kills") or m.get("player_kills") or 0)
        d = int(m.get("deaths") or m.get("player_deaths") or 0)
        a = int(m.get("assists") or m.get("player_assists") or 0)
        
        dmg = m.get("damage")
        obj_dmg = m.get("objective_damage")
        
        if dmg is None: dmg = m.get("hero_damage", 0)
        if obj_dmg is None: obj_dmg = m.get("obj_damage", 0)
        
        total_kills += k
        total_deaths += d
        total_assists += a
        total_damage += int(dmg)
        total_obj_damage += int(obj_dmg)
        
        won = m.get("won", False)
        if won:
            wins += 1
            net_wins += 1
        else:
            net_wins -= 1
            
        dt = ""
        start_time = m.get("start_time")
        if start_time:
            try:
                dt = datetime.utcfromtimestamp(int(start_time)).strftime("%b %d")
            except:
                pass
                
        net_wins_history.append({
            "date": dt, 
            "net_wins": net_wins, 
            "hero": m.get("hero_name", "Unknown")
        })
        
    avg_kills = total_kills / total_matches if total_matches > 0 else 0
    avg_deaths = total_deaths / total_matches if total_matches > 0 else 0
    avg_assists = total_assists / total_matches if total_matches > 0 else 0
    kda = (total_kills + total_assists) / max(1, total_deaths)
    win_rate = (wins / total_matches) * 100 if total_matches > 0 else 0
    
    return {
        "avg_kills": round(avg_kills, 1),
        "avg_deaths": round(avg_deaths, 1),
        "avg_assists": round(avg_assists, 1),
        "kda": round(kda, 2),
        "win_rate": round(win_rate, 1),
        "avg_damage": int(total_damage / max(1, total_matches)),
        "avg_obj_damage": int(total_obj_damage / max(1, total_matches)),
        "total_analyzed": total_matches,
        "net_wins_history": net_wins_history
    }


async def fetch_player_data(steam64: str, existing_cache: dict = None, force_pr_update: bool = False, deep: bool = False) -> dict:
    if existing_cache is None:
        existing_cache = {}
    existing_matches = existing_cache.get("match_history", [])
        
    steamid3 = steam64_to_steamid3(steam64)

    global_metrics, hero_stats, new_matches, steam_info = await asyncio.gather(
        get_global_player_metrics(),
        get_deadlock_hero_stats(steam64),
        get_player_match_history(steamid3, limit=None if deep else 10),
        get_steam_profile(steam64)
    )

    is_private = False
    if not hero_stats and not new_matches:
        is_private = True
        
    merged_dict = {m.get("match_id"): m for m in existing_matches if m.get("match_id")}
    for m in new_matches:
        if m.get("match_id"):
            merged_dict[m["match_id"]] = m
            
    match_history = list(merged_dict.values())
    match_history.sort(key=lambda x: int(x.get("start_time") or 0), reverse=True)

    # Backfill mode_label/rank_point_change on matches cached before this field existed
    for m in match_history:
        if "mode_label" not in m:
            m["mode_label"] = api.classify_match_mode(m)
            m["rank_point_change"] = (
                api.compute_rank_point_change(m) if m["mode_label"] == "Ranked" else None
            )
        if "is_calibration_match" not in m:
            m["is_calibration_match"] = (
                api.is_calibration_match(m) if m["mode_label"] == "Ranked" else False
            )

    total_matches = sum(h.get("matches_played", 0) for h in hero_stats)
    most_played = get_most_played_heros(hero_stats)

    # 1. Fetch the player's overall rank via the new /v1/players/{id}/rank
    #    endpoint (renamed from /rank-predict in the Matchmaking Update).
    #    This replaced the old per-hero MMR lookup as the source of truth for
    #    the profile badge -- it's an account-level rank, not hero-specific.
    valve_rank_data = await get_player_rank(steamid3)
    print(f"Fetched player rank for {steamid3}: {valve_rank_data}")

    # ---------------------------------------------------------------------
    # 429 / FETCH FAILURE FALLBACK: Use cached rank data if fetch returned None
    # ---------------------------------------------------------------------
    cached_valve_rank = existing_cache.get("valve_rank_data")
    effective_rank = valve_rank_data if valve_rank_data is not None else cached_valve_rank

    if valve_rank_data is None and cached_valve_rank:
        print(f"⚠️ Rank fetch returned None (HTTP 429 or network issue). Falling back to cached rank for {steamid3}.")

    # 2. Pass effective_rank directly into calcPr
    if force_pr_update or not existing_cache.get("pr_data"):
        pr_data = calcPr(hero_stats, steamid3=steamid3, player_rank=effective_rank, global_metrics=global_metrics)
    else:
        pr_data = existing_cache.get("pr_data")

    # 3. Resolve the rank widget (current badge, next badge, progress bar).
    #    Obscurus/unranked -> is_unranked=True, no bar. Progress points come
    #    straight from last_match.player_rank_final_flat_progress (see
    #    resolve_rank_widget's docstring), and the current badge image is
    #    the live /rank/image render for this account.
    rank_widget = resolve_rank_widget(effective_rank, steamid3=steamid3)
    rank_image = rank_widget["current_badge_image"]

    player_tags = generate_player_tags(hero_stats, match_history, total_matches)
    teammates = analyze_player_relationships(steamid3, match_history)
    hero_mastery = build_player_mastery(steamid3, match_history, most_played, top_n=5)

    return {
        "steam64": steam64,
        "steamid3": steamid3,
        "player": steam_info,
        "hero_stats": hero_stats,
        "match_history": match_history,
        "pr_data": pr_data,
        "valve_rank_data": effective_rank,  # Save effective rank so cache stays hydrated
        "most_played": most_played,
        "total_matches": total_matches,
        "rank_image": rank_image,
        "rank_widget": rank_widget,
        "player_tags": player_tags,
        "teammates": teammates,
        "hero_mastery": hero_mastery,
        "is_private": is_private,
    }

def get_or_refresh_player(steam64: str, force: bool = False, deep: bool = False) -> dict:
    steamid3 = steam64_to_steamid3(steam64)
    cache = load_player_cache(steamid3)

    if cache and not force and not is_cache_stale(cache):
        print(f"📦 Serving cached data for {steamid3}")
        
        modified_cache = False
        for m in cache.get("match_history", []):
            # Backfill is_indexed for existing old caches
            if "is_indexed" not in m:
                m["is_indexed"] = bool(m.get("player_kills") is not None or m.get("kills") is not None or m.get("match_duration_s") is not None or m.get("duration_s") is not None)
                modified_cache = True
                
            if not m.get("items_info"):
                enrich_match_with_local_data(m, steamid3)
                modified_cache = True
        
        cache["player_tags"] = generate_player_tags(
            cache.get("hero_stats", []), 
            cache.get("match_history", []), 
            cache.get("total_matches", 0)
        )

        if "teammates" not in cache:
            cache["teammates"] = analyze_player_relationships(steamid3, cache.get("match_history", []))
            modified_cache = True
            
        if "performance_stats" not in cache:
            cache["performance_stats"] = generate_performance_stats(cache.get("match_history", []))
            modified_cache = True
            
        if "mmr_history" not in cache:
            cache["mmr_history"] = []
            modified_cache = True

        if "hero_mastery" not in cache:
            cache["hero_mastery"] = build_player_mastery(
                steamid3,
                cache.get("match_history", []),
                cache.get("most_played", []),
                top_n=5
            )
            modified_cache = True

        # Backfill the rank widget (current/next badge + progress bar) for
        # caches written before this rework. Rebuilt from the rank data
        # already in the cache -- no extra API call needed.
        if "rank_widget" not in cache:
            cache["rank_widget"] = resolve_rank_widget(cache.get("valve_rank_data"), steamid3=steamid3)
            cache["rank_image"] = cache["rank_widget"]["current_badge_image"]
            modified_cache = True

        if modified_cache:
            save_player_cache(steamid3, cache)
            
        return cache

    print(f"🌐 Fetching fresh data for {steamid3} (force={force}, deep={deep})")
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        data = loop.run_until_complete(fetch_player_data(steam64, cache, force_pr_update=force, deep=deep))
    finally:
        loop.close()

    if not data.get("hero_stats") and not data.get("match_history"):
        if cache:
            print(f"⚠️ API returned incomplete data for {steamid3}. Preserving existing cache.")
            return cache

    data["performance_stats"] = generate_performance_stats(data.get("match_history", []))
    
    mmr_history = cache.get("mmr_history", []) if cache else []
    current_pr = data.get("pr_data", {}).get("overall_pr", 0)
    
    if current_pr > 0:
        current_time = time.time()
        if not mmr_history or mmr_history[-1].get("pr") != current_pr or (current_time - mmr_history[-1].get("timestamp", 0) > 86400):
            mmr_history.append({
                "timestamp": current_time,
                "date": datetime.utcfromtimestamp(current_time).strftime("%b %d %H:%M"),
                "pr": current_pr,
                "matches": data.get("total_matches", 0)
            })
            
    data["mmr_history"] = mmr_history

    save_player_cache(steamid3, data)
    return data


def update_patch_cache():
    global latest_patch_cache
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        latest_patch_cache = loop.run_until_complete(get_latest_patch())
        loop.close()
        print("✅ Latest patch loaded successfully")
    except Exception as e:
        print(f"⚠️ Could not load latest patch: {e}")
        latest_patch_cache = None

update_patch_cache()


@app.route('/api/random-hero')
def random_hero():
    selectable_heroes = [
        hero for hero in HEROS
        if hero.get("player_selectable", True)
    ]
    hero = random.choice(selectable_heroes)
    return jsonify({"name": hero["name"]})


@app.route('/api/metrics')
def api_metrics():
    return jsonify(get_metrics())


def format_souls(value):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return str(value) if value else '—'
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}m"
    if v >= 1_000:
        return f"{v/1_000:.1f}k"
    return str(v)

app.jinja_env.filters['format_souls'] = format_souls


def hero_icon(hero_name: str) -> str:
    if not hero_name or hero_name.lower() in ("unknown hero", "unknown", ""):
        return "images/unknown.png"
    # Files on disk keep the hero's original casing exactly (e.g. Abrams.png,
    # Grey_Talon.png, Lady_Geist.png, Mo_&_Krill.png) -- only spaces become
    # underscores. Lowercasing or rewriting "&" breaks the lookup entirely
    # on case-sensitive filesystems.
    #
    # NOTE: this returns a path WITHOUT a leading "/static/" -- every call
    # site in the templates does `src="/static/{{ ... | hero_icon }}"`, so
    # returning "/static/..." here as well was doubling the prefix into
    # "/static/static/images/heros/...", a path that never exists.
    name = hero_name.strip().replace(" ", "_")
    return f"images/heros/{name}.png"

app.jinja_env.filters['hero_icon'] = hero_icon


def item_image_url(item_id) -> str:
    try:
        iid = int(item_id)
    except (TypeError, ValueError):
        return ""
    info = get_item_info(iid)
    return info["image_url"] if info else ""


def item_name(item_id) -> str:
    try:
        iid = int(item_id)
    except (TypeError, ValueError):
        return str(item_id)
    info = get_item_info(iid)
    return info["name"] if info else str(item_id)

app.jinja_env.filters['item_image_url'] = item_image_url
app.jinja_env.filters['item_name'] = item_name

def _safe_int(v) -> int:
    try: return int(float(v))
    except: return 0

# ── Death Map ────────────────────────────────────────────────────────────
# NOTE ON CALIBRATION: Valve doesn't publish a documented world-coordinate ->
# minimap-image mapping for Deadlock, and there's no fixed bounding box we
# can trust blindly (even dedicated third-party replay tools like UltimateMove's
# analyzer ship a manual "Calibrate map" step for exactly this reason). Rather
# than guess a static world-space box - which is what produced the
# scattered/"random"-looking markers before - we auto-fit the transform to the
# actual range of death coordinates seen *in this match*. That guarantees
# every marker lands inside the image and that their positions *relative to
# each other* are correct, even though we can't promise pixel-perfect
# alignment with real landmarks (lanes/shops/etc.) without a proper Valve-
# provided calibration reference.
_DEATH_MAP_PADDING_PCT = 8.0  # keep markers off the image's outer edge


def _extract_death_events(p: dict) -> list:
    """Best-effort extraction of per-death world positions from a raw player
    blob. The public match-metadata endpoint doesn't document a stable field
    for this, so we try several plausible shapes and skip anything we can't
    parse rather than guessing. Returns a list of
    {x, y, time_s, killer_slot} in raw world coordinates.
    """
    candidates = (
        p.get("death_details")
        or p.get("deaths_details")
        or p.get("player_deaths_details")
        or p.get("death_events")
        or []
    )
    if not isinstance(candidates, list):
        return []

    events = []
    for d in candidates:
        if not isinstance(d, dict):
            continue

        pos = d.get("death_pos") or d.get("position") or d.get("pos")
        x = y = None
        if isinstance(pos, dict):
            x = pos.get("x")
            y = pos.get("y")
        elif isinstance(pos, (list, tuple)) and len(pos) >= 2:
            x, y = pos[0], pos[1]

        if x is None:
            x = d.get("pos_x") or d.get("x") or d.get("death_pos_x")
        if y is None:
            y = d.get("pos_y") or d.get("y") or d.get("death_pos_y")

        if x is None or y is None:
            continue

        try:
            x = float(x)
            y = float(y)
        except (TypeError, ValueError):
            continue

        time_s = (
            d.get("game_time_s")
            or d.get("time_s")
            or d.get("game_time")
            or d.get("death_time_s")
        )

        events.append({
            "x": x,
            "y": y,
            "time_s": int(time_s) if time_s is not None else None,
            "killer_slot": d.get("killer_player_slot"),
        })

    return events


def _fit_death_map_bounds(all_events: list) -> tuple:
    """Computes a square (min, max) world-space bounding box that contains
    every death event in this match, with padding so markers don't sit
    flush against the image edge. Falls back to a small default box if
    there's 0-1 events (nothing meaningful to fit)."""
    xs = [ev["x"] for ev in all_events]
    ys = [ev["y"] for ev in all_events]
    if len(all_events) < 2:
        return (-1000.0, 1000.0), (-1000.0, 1000.0)

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    # Use a single square span (not independent x/y scales) so the map's
    # aspect ratio isn't distorted - just centered on whichever axis has
    # more headroom.
    span = max(max_x - min_x, max_y - min_y, 1.0)
    pad = span * (_DEATH_MAP_PADDING_PCT / 100.0)
    span += pad * 2

    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    half = span / 2.0
    return (cx - half, cx + half), (cy - half, cy + half)


def _world_to_map_percent(x: float, y: float, x_bounds: tuple, y_bounds: tuple) -> tuple:
    """Maps raw world coords onto a 0-100 (left/top %) pair for CSS
    positioning over the minimap image, using this match's auto-fit bounds.
    Y is flipped since world-up is image-up (smaller top offset)."""
    x_min, x_max = x_bounds
    y_min, y_max = y_bounds
    x_span = max(x_max - x_min, 1.0)
    y_span = max(y_max - y_min, 1.0)
    px = ((x - x_min) / x_span) * 100.0
    py = ((y_max - y) / y_span) * 100.0
    return max(0.0, min(100.0, px)), max(0.0, min(100.0, py))


def _build_match_context(match_id: int, raw: dict, viewed_account_id: str = None) -> dict:
    mi = raw.get("match_info") or raw 

    duration_s = (mi.get("duration_s") or mi.get("match_duration_s")
                  or raw.get("duration_s") or 0)
    try:
        dur = int(duration_s)
        duration_display = f"{dur // 60}m {dur % 60:02d}s"
    except Exception:
        dur = 0
        duration_display = "N/A"

    dur_mins = max(1.0, dur / 60.0)

    start_time = mi.get("start_time") or raw.get("start_time")
    try:
        dt = datetime.utcfromtimestamp(int(start_time))
        date_display = dt.strftime("%B %d, %Y  %H:%M UTC")
    except Exception:
        date_display = "Unknown"

    winning_team = mi.get("winning_team")
    if winning_team is None:
        winning_team = raw.get("winning_team")
    try:
        winning_team = int(winning_team)
    except Exception:
        winning_team = 0

    gm_raw = mi.get("game_mode") or raw.get("game_mode") or ""
    game_mode_map = {
        "k_ECitadelMatchMode_Ranked": "Ranked",
        "k_ECitadelMatchMode_Unranked": "Unranked",
        "k_ECitadelMatchMode_Bot": "Bot",
        "k_ECitadelMatchMode_Sandbox": "Sandbox",
    }
    game_mode = game_mode_map.get(str(gm_raw), str(gm_raw).replace("k_ECitadelMatchMode_", "") if gm_raw else "")

    players_raw = mi.get("players") or raw.get("players") or []

    teams = {0: [], 1: []}
    slot_to_account = {}
    raw_death_events_by_account = {}
    for p in players_raw:
        team = p.get("team")
        if team is None:
            team = p.get("player_team")
        try:
            team = int(team) % 2 
        except Exception:
            team = 0

        kills   = p.get("kills") or p.get("player_kills") or 0
        deaths  = p.get("deaths") or p.get("player_deaths") or 0
        assists = p.get("assists") or p.get("player_assists") or 0
        nw      = p.get("net_worth") or p.get("networth") or 0
        lh      = p.get("last_hits") or None
        level   = p.get("level") or p.get("ending_level") or None

        damage = _safe_int(p.get("hero_damage") or p.get("damage") or 0)
        obj_damage = _safe_int(p.get("objective_damage") or p.get("obj_damage") or 0)

        stats_list = p.get("stats")
        if stats_list and isinstance(stats_list, list) and len(stats_list) > 0:
            last_stat = stats_list[-1]
            if last_stat.get("player_damage") is not None:
                damage = _safe_int(last_stat.get("player_damage"))
            if last_stat.get("objective_damage") is not None:
                obj_damage = _safe_int(last_stat.get("objective_damage"))

        items_info = p.get("items_info") or []
        if not items_info:
            items_info = get_final_items(p.get("items") or [])

        account_id_raw = p.get("account_id", "")
        if not account_id_raw or str(account_id_raw) == "0":
            steam64_id = ""
        else:
            try:
                steam64_id = str(int(account_id_raw) + 76561197960265728)
            except Exception:
                steam64_id = ""

        hero_id = p.get("hero_id") or p.get("hero")
        hero_name = p.get("hero_name")
        if not hero_name or str(hero_name).lower() in ("unknown", "unknown hero", "none"):
            if hero_id is not None:
                try:
                    hero_id_int = int(hero_id)
                    hero_name = API_HEROS_MAP.get(hero_id_int) or API_HEROS_MAP.get(str(hero_id_int))
                except (ValueError, TypeError):
                    hero_name = None
        if not hero_name:
            hero_name = "Unknown"

        # Track this player's slot so death events (which reference the
        # killer only by player_slot) can be resolved to an account/hero
        # once every player in the match has been processed.
        player_slot = p.get("player_slot")
        if player_slot is None:
            player_slot = p.get("slot")
        if player_slot is not None:
            slot_to_account[player_slot] = str(account_id_raw)

        raw_death_events_by_account[str(account_id_raw)] = {
            "events": _extract_death_events(p),
            "team": team,
            "hero_name": hero_name,
        }

        team_won = (team == winning_team)
        g_stats = {
            "hero_id": hero_id,
            "matches_played": 1,
            "wins": 1 if team_won else 0,
            "kills_per_min": int(kills) / dur_mins,
            "deaths_per_min": int(deaths) / dur_mins,
            "assists_per_min": int(assists) / dur_mins,
            "networth_per_min": (int(nw) if nw else 0) / dur_mins,
            "damage_per_min": damage / dur_mins,
            "obj_damage_per_min": obj_damage / dur_mins,
            "ending_level": int(level) if level is not None else 20,
        }
        try:
            g_pr_res = calcPr(g_stats, debug=False)
            game_pr = round(g_pr_res.get("overall_pr", 0) if isinstance(g_pr_res, dict) else 0)
        except Exception:
            game_pr = 0

        teams[team].append({
            "account_id": account_id_raw,
            "steam64":    steam64_id,
            "team":       team,
            "hero_id":    hero_id,
            "hero_name":  hero_name,
            "kills":      int(kills),
            "deaths":     int(deaths),
            "assists":    int(assists),
            "net_worth":  int(nw) if nw else 0,
            "last_hits":  int(lh) if lh is not None else None,
            "level":      int(level) if level is not None else None,
            "damage":     damage,
            "objective_damage": obj_damage,
            "items_info": items_info,
            "username":   str(account_id_raw),
            "game_pr":    game_pr,
        })

    for t in teams:
        teams[t].sort(key=lambda x: x["net_worth"], reverse=True)

    # Second pass: now that every player's slot/account/hero is known, resolve
    # each death event's killer_slot into an actual killer account + hero so
    # the map tooltip can show "Killer (Hero) | Victim (Hero) [time]".
    # Bounds are fit to this match's actual death coordinates (see note above
    # _fit_death_map_bounds) rather than a guessed fixed world box.
    _all_events = [ev for bundle in raw_death_events_by_account.values() for ev in bundle["events"]]
    _x_bounds, _y_bounds = _fit_death_map_bounds(_all_events)

    death_markers = []
    for victim_account_id, bundle in raw_death_events_by_account.items():
        for ev in bundle["events"]:
            left_pct, top_pct = _world_to_map_percent(ev["x"], ev["y"], _x_bounds, _y_bounds)
            killer_account_id = slot_to_account.get(ev["killer_slot"])
            killer_hero_name = None
            killer_team = None
            if killer_account_id is not None:
                killer_bundle = raw_death_events_by_account.get(killer_account_id)
                if killer_bundle:
                    killer_hero_name = killer_bundle["hero_name"]
                    killer_team = killer_bundle["team"]
            death_markers.append({
                "account_id":        victim_account_id,
                "team":              bundle["team"],
                "hero_name":         bundle["hero_name"],
                "time_s":            ev["time_s"],
                "left_pct":          round(left_pct, 2),
                "top_pct":           round(top_pct, 2),
                "killer_account_id": killer_account_id,
                "killer_hero_name":  killer_hero_name,
                "killer_team":       killer_team,
                # True environmental/neutral deaths (no killer_slot resolved
                # to a player on the opposing team) are shown as "Unknown".
                "is_environmental":  killer_account_id is None,
            })
    death_markers.sort(key=lambda dm: (dm["time_s"] is None, dm["time_s"]))

    all_players = teams[0] + teams[1]
    team0_kills = sum(p["kills"] for p in teams[0])
    team1_kills = sum(p["kills"] for p in teams[1])
    team0_souls = sum(p["net_worth"] for p in teams[0])
    team1_souls = sum(p["net_worth"] for p in teams[1])
    total_kills = team0_kills + team1_kills
    max_damage  = max((p["damage"] for p in all_players if p["damage"] is not None), default=1) or 1

    return {
        "match_id":           match_id,
        "duration_display":   duration_display,
        "date_display":       date_display,
        "game_mode":          game_mode,
        "winning_team":       winning_team,
        "teams":              teams,
        "team0_kills":        team0_kills,
        "team1_kills":        team1_kills,
        "team0_souls":        team0_souls,
        "team1_souls":        team1_souls,
        "total_kills":        total_kills,
        "total_players":      len(all_players),
        "max_damage":         max_damage,
        "viewed_account_id":  viewed_account_id or "",
        "death_markers":      death_markers,
        # NOTE: this is the highest-resolution public Deadlock minimap image
        # I could confirm (deadlock.wiki's own file, not a downscaled thumb).
        # I couldn't find a verified higher-res official asset via search -
        # if you have a better source (e.g. extracted directly from game
        # files, or a texture dump), swap the URL here or point it at a
        # local /static/images/ file instead.
        "map_image_url":      "https://deadlock.wiki/images/c/cf/Midtown0min.png",
    }


@app.route('/')
def index():
    trending_players = get_top_searched_players(limit=3)
    metrics = get_metrics()
    return render_template(
        'index.html',
        latest_patch=latest_patch_cache,
        trending_players=trending_players,
        metrics=metrics
    )


@app.route('/search', methods=['GET'])
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return redirect(url_for('index'))
    try:
        steam64 = resolve_steam_id(query)
        return redirect(url_for('player_profile', query=steam64))
    except Exception as e:
        print(f"Search error: {e}")
        return redirect(url_for('index'))


@app.route('/api/search-players')
def api_search_players():
    query = request.args.get('q', '').strip().lower()
    results = []

    if not query:
        trending = get_top_searched_players(limit=5)
        for t in trending:
            rank_url = normalize_rank_image_url(t.get('rank_image'))
            results.append({
                "steam64": t["steam64"],
                "personaname": t["personaname"],
                "avatar": t["avatar"],
                "pr": t["pr"],
                "main_hero": t["main_hero"],
                "rank_image": rank_url
            })
        return jsonify(results)

    try:
        for cache_file in PLAYER_DATA_DIR.glob('*.json'):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                player_info = data.get('player', {})
                personaname = player_info.get('personaname', '').lower()
                steam64 = data.get('steam64', '')
                
                if query in personaname or query == steam64:
                    most_played_list = data.get("most_played", [])
                    top_hero = most_played_list[0][0] if most_played_list else "Unknown Hero"
                    pr = round(data.get("pr_data", {}).get("overall_pr", 0))
                    
                    rank_url = normalize_rank_image_url(data.get("rank_image"))

                    results.append({
                        "steam64": steam64,
                        "personaname": player_info.get("personaname", "Unknown"),
                        "avatar": player_info.get("avatarfull", "/static/images/unknown.png"),
                        "pr": pr,
                        "main_hero": top_hero,
                        "rank_image": rank_url
                    })
            except Exception:
                continue
        
        results = sorted(results, key=lambda x: x['pr'], reverse=True)[:8]

    except Exception as e:
        print(f"Search API error: {e}")

    return jsonify(results)


@app.route('/changelog')
def changelog():

    return render_template('changelog.html')

@app.route('/leaderboard')
def leaderboard():
    selected_hero = request.args.get('hero', 'All')
    leaderboard_data = []

    for cache_file in PLAYER_DATA_DIR.glob('*.json'):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            player_info = data.get('player', {})
            steam64 = data.get('steam64', '')
            personaname = player_info.get('personaname', 'Unknown')
            avatar = player_info.get('avatarfull', '/static/images/unknown.png')
            pr_data = data.get('pr_data', {})

            most_played_list = data.get('most_played', [])
            top_hero = most_played_list[0][0] if most_played_list else "Unknown Hero"

            if selected_hero == 'All':
                pr_val = pr_data.get('overall_pr', 0)
                rank_image = normalize_rank_image_url(data.get('rank_image'))
            else:
                hero_pr_val = 0
                found = False
                for h in pr_data.get('heroes', []):
                    if h.get('hero_name') == selected_hero:
                        hero_pr_val = h.get('hero_pr', 0)
                        found = True
                        break
                
                if not found or hero_pr_val == 0:
                    continue

                pr_val = hero_pr_val
                pr_min = 100
                rank_index = int(math.floor((pr_val - pr_min) / 100))
                rank_index = max(0, min(65, rank_index))
                
                try:
                    rank_image = normalize_rank_image_url(badge_to_image(rank_index + 1))
                except Exception:
                    rank_image = "/static/images/unknown.png"

            if pr_val > 0:
                leaderboard_data.append({
                    'steam64': steam64,
                    'personaname': personaname,
                    'avatar': avatar,
                    'pr': round(pr_val),
                    'main_hero': top_hero,
                    'rank_image': rank_image
                })

        except Exception as e:
            print(f"Error parsing cache for leaderboard: {e}")
            continue

    leaderboard_data.sort(key=lambda x: x['pr'], reverse=True)

    for i, player in enumerate(leaderboard_data):
        player['rank'] = i + 1
    leaderboard_data = leaderboard_data[:100]

    selectable_heroes = sorted([h['name'] for h in HEROS if h.get("player_selectable", True)])

    return render_template(
        'leaderboard.html',
        leaderboard=leaderboard_data,
        heroes=selectable_heroes,
        selected_hero=selected_hero
    )


@app.route('/player/<query>')
def player_profile(query):
    try:
        steam64 = resolve_steam_id(query)
        data = get_or_refresh_player(steam64)

        last_updated = fetched_at_display(data)
        stale = is_cache_stale(data)

        search_count = increment_and_get_search_count(data["steam64"])

        most_played_list = data.get("most_played", [])
        top_hero_name = most_played_list[0][0] if most_played_list else "Unknown Hero"
        
        from api import get_hero_background_url, get_hero_name_logo_url
        bg_image = get_hero_background_url(top_hero_name)
        logo_image = get_hero_name_logo_url(top_hero_name)

        return render_template(
            'player.html',
            query=steam64,
            last_updated=last_updated,
            bg_image=bg_image,
            search_count=search_count,
            logo_image=logo_image,
            cache_stale=stale,
            **{k: data.get(k) for k in (
                'player', 'pr_data', 'most_played',
                'hero_stats', 'match_history', 'total_matches', 'rank_image', 'rank_widget',
                'is_private', 'player_tags', 'teammates', 'performance_stats', 'mmr_history',
                'hero_mastery'
            )}
        )

    except Exception as e:
        print(f"Player profile error: {e}")
        return render_template(
            'player.html',
            query=query,
            error=str(e),
            player={},
            pr_data={},
            most_played=[],
            hero_stats=[],
            match_history=[],
            total_matches=0,
            rank_image=None,
            rank_widget=None,
            last_updated=None,
            cache_stale=False,
            is_private=False,
            player_tags=[],
            teammates=[],
            performance_stats=None,
            mmr_history=[],
            hero_mastery=[]
        )


@app.route('/player/<query>/refresh')
def refresh_player(query):
    deep = request.args.get('deep') == 'true'
    try:
        steam64 = resolve_steam_id(query)
        get_or_refresh_player(steam64, force=True, deep=deep)
    except Exception as e:
        print(f"Refresh error: {e}")
    return redirect(url_for('player_profile', query=query, t=int(time.time())))


@app.route('/match/<int:match_id>')
def match_detail(match_id):
    referrer_query = request.args.get('from', '') 

    try:
        raw = get_or_fetch_match(match_id)
        if not raw:
            return render_template(
                'match.html',
                match_id=match_id,
                referrer_query=referrer_query,
                error="Match not found or not yet indexed by the Deadlock API.",
            )

        viewed_account_id = ""
        if referrer_query:
            try:
                viewed_account_id = steam64_to_steamid3(resolve_steam_id(referrer_query))
            except Exception:
                pass

        ctx = _build_match_context(match_id, raw, viewed_account_id)

        import requests as _req
        all_account_ids = [
            str(p["account_id"])
            for team in ctx["teams"].values()
            for p in team
            if p.get("account_id")
        ]
        
        player_profiles = {}
        try:
            steam64_ids = [str(int(aid) + 76561197960265728) for aid in all_account_ids if aid.isdigit()]
            if steam64_ids:
                chunk = ",".join(steam64_ids[:100])
                resp = _req.get(
                    f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
                    f"?key={STEAM_API_KEY}&steamids={chunk}",
                    timeout=10
                )
                profiles = resp.json().get("response", {}).get("players", [])
                for prof in profiles:
                    aid = str(int(prof["steamid"]) - 76561197960265728)
                    player_profiles[aid] = {
                        "username": prof.get("personaname", f"#{aid}"),
                        "avatar": prof.get("avatarfull", "/static/images/unknown.png")
                    }
        except Exception as e:
            print(f"Username resolution error: {e}")

        for team in ctx["teams"].values():
            for p in team:
                aid = str(p.get("account_id", ""))
                prof_data = player_profiles.get(aid, {})
                p["username"] = prof_data.get("username", f"#{aid}")
                p["avatar"] = prof_data.get("avatar", "/static/images/unknown.png")
                
                cache = load_player_cache(aid)
                if cache:
                    account_pr = round(cache.get("pr_data", {}).get("overall_pr", 0))
                    p["account_pr"] = account_pr if account_pr > 0 else None
                    p["is_private"] = cache.get("is_private", False)
                    p["rank_image"] = normalize_rank_image_url(cache.get("rank_image")) if cache.get("rank_image") else None

                    mmr_val = cache.get("mmr") or cache.get("pr_data", {}).get("mmr")
                    if not mmr_val and account_pr:
                        mmr_val = int(account_pr * 0.85)
                    p["mmr"] = mmr_val or 0
                else:
                    p["account_pr"] = None
                    p["is_private"] = False
                    p["mmr"] = int(p.get("game_pr", 0) * 0.85)
                    if p["mmr"] > 0:
                        badge = mmr_to_badge(p["mmr"])
                        p["rank_image"] = normalize_rank_image_url(badge_to_image(badge))
                    else:
                        p["rank_image"] = get_rank_badge_image("Obscurus")

                game_pr = p.get("game_pr", 0)
                if p["account_pr"] is not None:
                    p["vs_account_pr"] = game_pr - p["account_pr"]
                else:
                    p["vs_account_pr"] = None

                team_won = (p.get("team") == ctx.get("winning_team"))
                base_delta = 22 if team_won else -22
                if p["vs_account_pr"] is not None:
                    perf_adj = max(-15, min(15, int(p["vs_account_pr"] / 40)))
                    p["pr_gain_loss"] = base_delta + perf_adj
                else:
                    p["pr_gain_loss"] = base_delta

        # Attach display names + hero icons to death markers now that
        # usernames are resolved, so the map tooltip can render
        # "Killer (icon) | Victim (icon) [time]" without extra lookups.
        username_by_account = {
            str(p.get("account_id", "")): p.get("username", f"#{p.get('account_id', '')}")
            for team in ctx["teams"].values()
            for p in team
        }
        for dm in ctx.get("death_markers", []):
            dm["username"] = username_by_account.get(dm["account_id"], f"#{dm['account_id']}")
            dm["hero_icon"] = f"/static/{hero_icon(dm['hero_name'])}" if dm.get("hero_name") else "/static/images/unknown.png"

            killer_account_id = dm.get("killer_account_id")
            if killer_account_id is not None:
                dm["killer_username"] = username_by_account.get(str(killer_account_id), f"#{killer_account_id}")
                dm["killer_hero_icon"] = (
                    f"/static/{hero_icon(dm['killer_hero_name'])}" if dm.get("killer_hero_name") else "/static/images/unknown.png"
                )
            else:
                dm["killer_username"] = "Unknown"
                dm["killer_hero_icon"] = "/static/images/unknown.png"

            mins = (dm["time_s"] or 0) // 60
            secs = (dm["time_s"] or 0) % 60
            dm["time_display"] = f"{mins:02d}:{secs:02d}" if dm.get("time_s") is not None else "--:--"

        return render_template(
            'match.html',
            referrer_query=referrer_query,
            **ctx,
        )

    except Exception as e:
        print(f"Match detail error for {match_id}: {e}")
        return render_template(
            'match.html',
            match_id=match_id,
            referrer_query=referrer_query,
            error=f"Failed to load match: {e}",
        )


@app.route('/heroes')
def heroes_gallery():
    with open(HEROS_FILE, 'r', encoding='utf-8') as f:
        heroes_data = json.load(f)
    return render_template('heroes.html', heroes=heroes_data)


@app.route('/heroes/<hero_name>')
def hero_detail(hero_name):
    with open(HEROS_FILE, 'r', encoding='utf-8') as f:
        heroes_data = json.load(f)
    
    hero = next((h for h in heroes_data if h['name'].lower() == hero_name.lower()), None)
    if not hero:
        return "Hero not found in roster.", 404
        
    return render_template('hero_detail.html', hero=hero)

#  live matches

def _resolve_live_predictions(limit: int = 10) -> list:
    """Fetches live matches and reshapes them into the card/modal format
    expected by prediction.html (tier, confidence, avg mmr/pr, per-player rows)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        matches = loop.run_until_complete(get_active_matches(limit=limit))
    finally:
        loop.close()

    # Batch-resolve usernames/avatars for every player across every match in one Steam call
    all_account_ids = {
        str(p["account_id"])
        for m in matches
        for p in (m["team0"]["players"] + m["team1"]["players"])
        if p.get("account_id")
    }
    profiles = {}
    if all_account_ids:
        try:
            import requests as _req
            steam64_ids = [str(int(aid) + 76561197960265728) for aid in all_account_ids if aid.isdigit()]
            for i in range(0, len(steam64_ids), 100):
                chunk = ",".join(steam64_ids[i:i + 100])
                resp = _req.get(
                    f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
                    f"?key={STEAM_API_KEY}&steamids={chunk}",
                    timeout=10
                )
                for prof in resp.json().get("response", {}).get("players", []):
                    aid = str(int(prof["steamid"]) - 76561197960265728)
                    profiles[aid] = {
                        "username": prof.get("personaname", f"#{aid}"),
                        "avatar": prof.get("avatarfull", "/static/images/unknown.png"),
                    }
        except Exception as e:
            print(f"Live predictions - username resolution error: {e}")

    def build_team(team_players: list) -> tuple[list, int, int]:
        rows, mmr_total, pr_total, pr_count = [], 0, 0, 0
        for p in team_players:
            aid = str(p.get("account_id", ""))
            cache = load_player_cache(aid) or {}
            account_pr = round(cache.get("pr_data", {}).get("overall_pr", 0))
            mmr_val = cache.get("mmr") or cache.get("pr_data", {}).get("mmr") or 0
            if not mmr_val and account_pr:
                mmr_val = int(account_pr * 0.85)

            prof = profiles.get(aid, {})
            rows.append({
                "account_id": aid,
                "username": prof.get("username", f"#{aid}"),
                "hero_name": p.get("hero_name", "Unknown Hero"),
                "hero_icon": f"/static/{hero_icon(p.get('hero_name'))}",
                "mmr": mmr_val,
                "pr": account_pr or None,
                "kills": p.get("kills", 0),
                "deaths": p.get("deaths", 0),
                "assists": p.get("assists", 0),
                "level": p.get("level", 1),
            })
            mmr_total += mmr_val
            if account_pr:
                pr_total += account_pr
                pr_count += 1

        n = len(team_players) or 1
        avg_mmr = f"{round(mmr_total / n):,}" if mmr_total else "---"
        avg_pr = round(pr_total / pr_count) if pr_count else None
        return rows, avg_mmr, avg_pr

    predictions_data = []
    for m in matches:
        team0_rows, team0_avg_mmr, team0_avg_pr = build_team(m["team0"]["players"])
        team1_rows, team1_avg_mmr, team1_avg_pr = build_team(m["team1"]["players"])
        amber_pred = m["team0"]["win_prediction"]
        sapphire_pred = m["team1"]["win_prediction"]

        team0_nw = m["team0"]["net_worth"]
        team1_nw = m["team1"]["net_worth"]
        total_nw = team0_nw + team1_nw
        team0_soul_pct = round((team0_nw / total_nw) * 100, 1) if total_nw else 50.0
        team1_soul_pct = round(100 - team0_soul_pct, 1) if total_nw else 50.0

        # Momentum bar: a centered "tug of war" indicator. offset = how far
        # the lead has pushed off the 50/50 midpoint.
        lead_team = m.get("lead_team", "Tie")
        offset = abs(team0_soul_pct - 50)
        if lead_team == "Amber":
            momentum_bar_left, momentum_bar_width = 50 - offset, offset
        elif lead_team == "Sapphire":
            momentum_bar_left, momentum_bar_width = 50, offset
        else:
            momentum_bar_left, momentum_bar_width = 50, 0

        predictions_data.append({
            "match_id": m["match_id"],
            "tier": m.get("match_mode", "LIVE"),
            "confidence": round(max(amber_pred, sapphire_pred)),
            "predicted_winner": 0 if amber_pred >= sapphire_pred else 1,
            "team0_avg_mmr": team0_avg_mmr,
            "team1_avg_mmr": team1_avg_mmr,
            "team0_avg_pr": team0_avg_pr if team0_avg_pr is not None else "---",
            "team1_avg_pr": team1_avg_pr if team1_avg_pr is not None else "---",
            "teams": [team0_rows, team1_rows],
            "spectators": m.get("spectators", 0),
            "game_time": m.get("time_display", "--:--"),
            "team0_net_worth": team0_nw,
            "team1_net_worth": team1_nw,
            "soul_delta": m.get("net_worth_delta", 0),
            "team0_soul_pct": team0_soul_pct,
            "team1_soul_pct": team1_soul_pct,
            "momentum_status": m.get("momentum_status", "Even Game"),
            "lead_team": lead_team,
            "momentum_bar_left": momentum_bar_left,
            "momentum_bar_width": momentum_bar_width,
        })

    return predictions_data

@app.route('/predictions')
def prediction_page():
    try:
        predictions_data = _resolve_live_predictions(limit=10)
    except Exception as e:
        print(f"Prediction page error: {e}")
        predictions_data = []

    return render_template('prediction.html', predictions=predictions_data)


@app.route('/api/active-matches')
def api_active_matches():
    """JSON API endpoint for client-side live polling updates."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        matches = loop.run_until_complete(get_active_matches(limit=30))
    finally:
        loop.close()
    return jsonify({"success": True, "matches": matches})


@app.route('/api/active-match/<int:match_id>')
def api_active_match_detail(match_id):
    """JSON API endpoint returning detailed telemetry for card inspection."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        matches = loop.run_until_complete(get_active_matches(limit=50))
    finally:
        loop.close()
        
    match_detail = next((m for m in matches if str(m["match_id"]) == str(match_id)), None)
    if not match_detail:
        return jsonify({"success": False, "error": "Match concluded or unavailable"}), 404
        
    return jsonify({"success": True, "match": match_detail})


# STEAM



if __name__ == '__main__':
    print("🚀 PredictDL starting...")
    warm_rank_tier_assets_cache()
    app.run(debug=True, port=5000)