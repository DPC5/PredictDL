import requests
import re
import json
import asyncio
from curl_cffi.requests import AsyncSession
import time
import math
from pathlib import Path
from datetime import datetime
import hashlib as _hashlib
import colorsys

CURRENT_DIR = Path(__file__).parent
BASE_DIR = CURRENT_DIR.parent
CONFIG_FILE = BASE_DIR / 'data' / 'config.json'
STATS_FILE  = BASE_DIR / 'data' / 'stats.json'
HEROS_FILE  = BASE_DIR / 'data' / 'heros.json'
ITEMS_FILE  = BASE_DIR / 'data' / 'items.json'
PLAYER_DATA_DIR = BASE_DIR / 'data' / 'player_data'
MATCH_DATA_DIR = BASE_DIR / 'data' / 'match_data'
METRICS_CACHE_FILE = BASE_DIR / 'data' / 'player_metrics_cache.json'
METRICS_TTL_SECONDS = 3600

with open(CONFIG_FILE, 'r') as file:
    config = json.load(file)

with open(HEROS_FILE, 'r', encoding="utf-8") as file:
    heros = json.load(file)

HEROS = {hero["id"]: hero["name"] for hero in heros}


def _wiki_image_url(item_name: str) -> str:
    filename = item_name.replace(" ", "_") + ".png"
    md5 = _hashlib.md5(filename.encode()).hexdigest()
    return f"https://deadlock.wiki/images/{md5[0]}/{md5[:2]}/{filename}"


ITEMS: dict = {}
try:
    with open(ITEMS_FILE, 'r', encoding="utf-8") as file:
        items_raw = json.load(file)
    for item in items_raw:
        if not item.get("shopable"):
            continue
        item_id = item.get("id")
        if item_id is None:
            continue
        name = item.get("name") or ""
        if not name:
            continue
        ITEMS[item_id] = {
            "name":      name,
            "image_url": _wiki_image_url(name),
            "tier":      item.get("item_tier"),
            "slot":      item.get("item_slot_type"),
        }
except FileNotFoundError:
    pass


def get_item_info(item_id: int):
    return ITEMS.get(item_id)


def get_final_items(player_items: list) -> list:
    if not player_items:
        return []

    seen: dict = {}
    for entry in player_items:
        iid = entry.get("item_id")
        if iid and entry.get("sold_time_s", 0) == 0:
            seen[iid] = entry 

    result = []
    for iid in seen:
        info = get_item_info(iid)
        if info is None:
            continue  
        result.append({
            "item_id":   iid,
            "name":      info["name"],
            "image_url": info["image_url"],
            "tier":      info.get("tier"),
            "slot":      info.get("slot"),
        })
    return result

STEAM_API_KEY = config.get('STEAM_API_KEY')


async def fetch_async_json(url, session, retries=3, base_delay=1.0):
    """Helper to fetch JSON with exponential backoff for rate limits/403s."""
    for attempt in range(retries):
        try:
            response = await session.get(url, timeout=15)
            
            if response.status_code in (429, 403):
                if attempt < retries - 1:
                    await asyncio.sleep(base_delay * (2 ** attempt))
                    continue
            if response.status_code != 200:
                print(f"Fetch failed {url}: HTTP {response.status_code}")
                return None
                
            return response.json()
            
        except Exception as e:
            if attempt < retries - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
            else:
                print(f"Failed fetching {url}: {e}")
                return None
    return None


async def get_steam_profile(steam64: str) -> dict:
    try:
        async with AsyncSession(impersonate="chrome") as session:
            url = f"http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={STEAM_API_KEY}&steamids={steam64}"
            data = await fetch_async_json(url, session)
            if data and "response" in data and "players" in data["response"]:
                players = data["response"]["players"]
                if players:
                    return players[0]
    except Exception as e:
        print(f"Error fetching steam profile: {e}")
    return {"personaname": "Unknown", "avatarfull": "/static/images/unknown.png"}


def resolve_steam_id(input_value: str) -> str:
    if input_value.isdigit() and len(input_value) == 17:
        return input_value

    if "steamcommunity.com" in input_value:
        vanity_match = re.search(r"/id/([^/]+)/?", input_value)
        numeric_match = re.search(r"/profiles/(\d+)/?", input_value)

        if numeric_match:
            return numeric_match.group(1)

        elif vanity_match:
            vanity_name = vanity_match.group(1)
            url = f"https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/?key={STEAM_API_KEY}&vanityurl={vanity_name}"

            response = requests.get(url, timeout=10)
            data = response.json()

            if data["response"]["success"] == 1:
                return data["response"]["steamid"]
            else:
                raise ValueError(f"Could not resolve vanity URL: {vanity_name}")

    url = f"http://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/?key={STEAM_API_KEY}&vanityurl={input_value}"
    response = requests.get(url, timeout=10)
    data = response.json()

    if data["response"]["success"] == 1:
        return data["response"]["steamid"]

    raise ValueError(f"Could not resolve Steam ID: {input_value}")


def steam64_to_steamid3(steam64: str) -> str:
    steam64_int = int(steam64)
    account_id = steam64_int - 76561197960265728
    return f"{account_id}"

def steamid3_to_steam64(steamid3: str | int) -> str:
    """Converts 32-bit Steam Account ID to 64-bit Steam ID."""
    return str(int(steamid3) + 76561197960265728)

async def get_deadlock_hero_stats(input_value: str):
    steam64 = resolve_steam_id(input_value)
    steamid3 = steam64_to_steamid3(steam64)
    try:
        async with AsyncSession(impersonate="chrome") as session:
            url = f"https://api.deadlock-api.com/v1/players/hero-stats?account_ids={steamid3}"
            data = await fetch_async_json(url, session)
        return data if data else []
    except Exception as e:
        print(f"Error fetching hero stats: {e}")
        return []

def enrich_match_with_local_data(match: dict, steamid3: str):
    """Reads local match cache to apply deep stats (damage/items) avoiding network requests."""
    match_id = match.get("match_id")
    
    match["objective_damage"] = (
        match.get("boss_damage")
        or match.get("objective_damage")
        or match.get("obj_damage")
        or 0
    )

    if match_id:
        cached_file = MATCH_DATA_DIR / f"{match_id}.json"
        if cached_file.exists():
            try:
                with open(cached_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                match_info = meta.get("match_info") or meta
                players = match_info.get("players") or []
                for p in players:
                    if str(p.get("account_id")) == str(steamid3):
                        stats_list = p.get("stats", [])
                        if stats_list and isinstance(stats_list, list):
                            last_stat = stats_list[-1]
                            match["damage"] = (
                                last_stat.get("player_damage") 
                                or last_stat.get("hero_damage") 
                                or 0
                            )
                            match["objective_damage"] = (
                                last_stat.get("boss_damage")
                                or last_stat.get("objective_damage")
                                or last_stat.get("obj_damage")
                                or 0
                            )
                        else:
                            match["damage"] = (
                                p.get("damage") 
                                or p.get("hero_damage") 
                                or p.get("player_damage") 
                                or 0
                            )
                            match["objective_damage"] = (
                                p.get("boss_damage")
                                or p.get("objective_damage")
                                or p.get("obj_damage")
                                or p.get("damage_to_structures")
                                or 0
                            )
                        if not match.get("items"):
                            match["items"] = p.get("items") or []
                        break
            except Exception as e:
                print(f"Failed loading local cache for match {match_id}: {e}")

    raw_items = match.get("items") or []
    match["items_info"] = get_final_items(raw_items)


STREET_BRAWL_TOKENS = ("brawl",)


def classify_match_mode(match: dict) -> str:
    """
    Buckets a match into 'Ranked', 'Unranked', or 'Street Brawl'.
    Relies on mode-specific data fields being populated (not null)
    rather than brittle enum parsing.
    """
    # 1. Check if Street Brawl fields are set
    if match.get("brawl_avg_round_time_s") is not None or match.get("brawl_score_team0") is not None:
        return "Street Brawl"
        
    # 2. Check if Ranked fields are set
    if match.get("ranked_delta") is not None or match.get("ranked_display_badge") is not None:
        return "Ranked"

    # 3. Legacy string check fallback (just in case the API changes later)
    match_mode_raw = match.get("match_mode_parsed") or match.get("match_mode") or ""
    match_mode_str = str(match_mode_raw).lower()
    
    if "brawl" in match_mode_str:
        return "Street Brawl"
    if "ranked" in match_mode_str and "unranked" not in match_mode_str:
        return "Ranked"

    # Default to Unranked if no specific fields are populated
    return "Unranked"

def compute_rank_point_change(match: dict):
    """
    Best-effort extraction of the ranked-points gained/lost on a match.
    The deadlock-api match-history endpoint reports this as `ranked_delta`;
    a few legacy/alternate field names are checked as a fallback. Returns
    None if none are present (in which case the UI simply omits the +/- RP
    indicator).
    """
    for key in (
        "ranked_delta",
        "rank_change",
        "score_change",
        "player_score_change",
        "delta_score",
        "mmr_change",
        "rank_points_change",
    ):
        val = match.get(key)
        if val is not None:
            try:
                return int(round(float(val)))
            except (TypeError, ValueError):
                continue
    return None


def is_calibration_match(match: dict) -> bool:
    """Whether this ranked match was one of the player's calibration games."""
    return bool(match.get("ranked_calibration_match"))


async def get_player_match_history(steamid3: str, limit: int = 10) -> list:
    try:
        async with AsyncSession(impersonate="chrome") as session:
            url = f"https://api.deadlock-api.com/v1/players/{steamid3}/match-history"
            data = await fetch_async_json(url, session)
            
            if not data:
                return []
    except Exception as e:
        print(f"Error fetching match history: {e}")
        return []

    matches = data if isinstance(data, list) else data.get("matches", [])
    
    if limit:
        matches = matches[:limit]

    for match in matches:
        match_id = match.get("match_id")
        hero_id = match.get("hero_id")
        
        # Check if match has been properly indexed by determining if it contains core statistics.
        has_kills = match.get("player_kills") is not None or match.get("kills") is not None
        has_duration = match.get("match_duration_s") is not None or match.get("duration_s") is not None
        match["is_indexed"] = bool(has_kills or has_duration)

        match["hero_name"] = HEROS.get(hero_id, "Unknown Hero") if hero_id else "Unknown Hero"

        duration_s = match.get("match_duration_s") or match.get("duration_s")
        if duration_s:
            mins = int(duration_s) // 60
            secs = int(duration_s) % 60
            match["duration_display"] = f"{mins}m {secs:02d}s"
        else:
            match["duration_display"] = "N/A"

        start_time = match.get("start_time")
        if start_time:
            try:
                dt = datetime.utcfromtimestamp(int(start_time))
                match["date_display"] = dt.strftime("%b %d, %Y")
                match["time_display"] = dt.strftime("%H:%M UTC")
            except Exception:
                match["date_display"] = ""
                match["time_display"] = ""

        if match.get("player_kills") is not None and match.get("kills") is None:
            match["kills"] = match["player_kills"]
        if match.get("player_deaths") is not None and match.get("deaths") is None:
            match["deaths"] = match["player_deaths"]
        if match.get("player_assists") is not None and match.get("assists") is None:
            match["assists"] = match["player_assists"]

        pt = match.get("player_team")
        mr = match.get("match_result")
        wt = match.get("winning_team")

        winning_team = -1
        if wt is not None:
            try: winning_team = int(wt)
            except: pass
        elif mr is not None:
            try: winning_team = int(mr)
            except: pass

        if pt is not None and winning_team != -1:
            try:
                match["won"] = (int(pt) == winning_team)
            except:
                match["won"] = False
        else:
            match["won"] = False

        match["damage"] = (
            match.get("player_damage") 
            or match.get("hero_damage") 
            or 0
        )

        match["mode_label"] = classify_match_mode(match)
        match["rank_point_change"] = (
            compute_rank_point_change(match) if match["mode_label"] == "Ranked" else None
        )
        match["is_calibration_match"] = (
            is_calibration_match(match) if match["mode_label"] == "Ranked" else False
        )
        
        match["objective_damage"] = (
            match.get("boss_damage")
            or match.get("objective_damage")
            or match.get("obj_damage")
            or match.get("damage_to_structures")
            or 0
        )
        
        enrich_match_with_local_data(match, steamid3)

    return matches


def get_most_played_heros(input_value):
    lst = []
    for hero in input_value:
        hero_name = HEROS.get(hero['hero_id'], "Unknown Hero")
        matches = hero.get('matches_played', 0)
        lst.append((hero_name, matches, hero['hero_id']))
    return sorted(lst, key=lambda x: x[1], reverse=True)


def get_hero_stats(input_value, hero_id):
    for hero in input_value:
        if hero['hero_id'] == hero_id:
            return hero
    return None


async def get_hero_rank(hero_id: int, steamid3: str):
    async with AsyncSession(impersonate="chrome") as session:
        url = f"https://api.deadlock-api.com/v1/players/mmr/{hero_id}?account_ids={steamid3}"
        data = await fetch_async_json(url, session)
        
    if not data:
        return None

    return data[0] if isinstance(data, list) and len(data) > 0 else data


RANK_NAMES = [
    "Obscurus", "Initiate", "Seeker", "Alchemist", "Arcanist",
    "Ritualist", "Emissary", "Archon", "Oracle", "Phantom",
    "Ascendant", "Eternus"
]

def mmr_to_badge(mmr_score: float) -> int:
    if mmr_score < 100:
        return 0
    rank_index = int(math.floor((mmr_score - 100) / 100))
    return max(1, min(66, rank_index + 1))


def resolve_valve_rank(player_rank):
    """
    Parses player_rank (dict, int, or float) and returns:
    (badge_num, rank_name_str, division, tier)
    """
    division = 0
    tier = 0
    badge_num = 0

    if isinstance(player_rank, dict):
        # 1. Support the new Matchmaking API keys 'rank' and 'subrank'
        division = int(player_rank.get("rank") or player_rank.get("division") or player_rank.get("rank_division") or 0)
        tier = int(player_rank.get("subrank") or player_rank.get("division_tier") or player_rank.get("tier") or 0)
        
        if division > 0:
            # Convert Valve's 1-11 rank scale into the continuous 1-66 badge scale
            badge_num = ((division - 1) * 6) + max(1, min(6, tier if tier > 0 else 1))
        else:
            # Fallback for dictionaries that only supply 'badge'
            raw_badge = int(player_rank.get("badge") or 0)
            if raw_badge > 10:
                # Parse Valve's internal format (e.g., 82 -> Tier 8, Subrank 2)
                div = raw_badge // 10
                sub = raw_badge % 10
                badge_num = ((div - 1) * 6) + max(1, min(6, sub if sub > 0 else 1))
            else:
                badge_num = raw_badge

    elif isinstance(player_rank, (int, float)):
        val = float(player_rank)
        if 1 <= val <= 66:
            badge_num = int(val)
        elif val > 66:
            badge_num = mmr_to_badge(val)
        else:
            badge_num = 0

    if badge_num > 0:
        badge_num = max(1, min(66, badge_num))
        division = ((badge_num - 1) // 6) + 1
        tier = ((badge_num - 1) % 6) + 1
        if division < len(RANK_NAMES):
            rank_name = f"{RANK_NAMES[division]} {tier}"
        else:
            rank_name = f"Eternus {tier}"
    else:
        rank_name = "Obscurus (Unranked)"

    return badge_num, rank_name, division, tier


# ── Rank Badges & RP (Matchmaking Update) ──────────────────────────────────
# As of the "Matchmaking Update" (2026-07-30) patch, the game no longer ships
# per-subrank badge art. /v1/assets/ranks now serves ONE large/large_webp
# image per tier (Obscurus, Initiate, ... Eternus), plus a new chalk/chalk_webp
# variant. The old /v1/players/{id}/rank-predict endpoint was renamed to
# /v1/players/{id}/rank.

RANK_ASSETS_CACHE_FILE = BASE_DIR / 'data' / 'rank_assets_cache.json'
RANK_ASSETS_TTL_SECONDS = 24 * 60 * 60  # badge art is static between patches


def _load_rank_assets_cache() -> dict | None:
    try:
        if RANK_ASSETS_CACHE_FILE.exists():
            with open(RANK_ASSETS_CACHE_FILE, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            if time.time() - cached.get('_fetched_at', 0) < RANK_ASSETS_TTL_SECONDS:
                return cached.get('tiers') or None
    except Exception as e:
        print(f"Error reading rank assets cache: {e}")
    return None


def _save_rank_assets_cache(tiers: dict):
    try:
        RANK_ASSETS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RANK_ASSETS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'_fetched_at': time.time(), 'tiers': tiers}, f)
    except Exception as e:
        print(f"Error writing rank assets cache: {e}")


async def get_rank_tier_assets() -> dict:
    """
    Fetches badge art from GET /v1/assets/ranks and returns it keyed by tier
    display name, e.g.:
        {"Obscurus": {"large_webp": "...", "large": "...", "chalk_webp": "...", "chalk": "..."}, ...}

    Cached to disk for RANK_ASSETS_TTL_SECONDS so we don't re-fetch on every
    page load. NOTE: the published docs snapshot didn't include a concrete
    example payload for this endpoint, so the parsing below is deliberately
    defensive (tries a few plausible key names). If tiers comes back empty,
    check the printed raw payload in the logs and adjust the key names here.
    """
    cached = _load_rank_assets_cache()
    if cached:
        return cached

    tiers: dict = {}
    data = None
    try:
        async with AsyncSession(impersonate="chrome") as session:
            data = await fetch_async_json("https://api.deadlock-api.com/v1/assets/ranks", session)

        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            entries = data.get("ranks") or data.get("data") or list(data.values())
        else:
            entries = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = (
                entry.get("name")
                or entry.get("tier_name")
                or entry.get("rank_name")
                or entry.get("tier")
                or ""
            )
            images = entry.get("images") if isinstance(entry.get("images"), dict) else entry
            large_webp = images.get("large_webp") or entry.get("image_large_webp")
            large = images.get("large") or entry.get("image_large") or large_webp
            chalk_webp = images.get("chalk_webp") or entry.get("image_chalk_webp")
            chalk = images.get("chalk") or entry.get("image_chalk") or chalk_webp

            if name and (large_webp or large):
                tiers[str(name)] = {
                    "large_webp": large_webp or large,
                    "large": large or large_webp,
                    "chalk_webp": chalk_webp or chalk,
                    "chalk": chalk or chalk_webp,
                }

        if tiers:
            _save_rank_assets_cache(tiers)
        else:
            print(f"⚠️ /v1/assets/ranks returned no usable tier art. Raw payload (truncated): {str(data)[:500]}")

    except Exception as e:
        print(f"Error fetching rank tier assets: {e}")

    return tiers


async def get_ranked_seasons() -> list:
    """Fetches GET /v1/assets/ranked-seasons (season id/name/date ranges)."""
    try:
        async with AsyncSession(impersonate="chrome") as session:
            data = await fetch_async_json("https://api.deadlock-api.com/v1/assets/ranked-seasons", session)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error fetching ranked seasons: {e}")
        return []


async def get_player_rank(steamid3: str) -> dict | None:
    """
    Fetches the player's current rank via GET /v1/players/{account_id}/rank.

    Per the current API docs, this returns the rank the player ended their
    latest *ranked* match at (the rank they entered that match with plus the
    progress it awarded). A subrank spans 1000 progress points, so a single
    match can move the badge.

    Response schema:
        {
          "badge": int,        # combined badge number (0 = Obscurus/unranked)
          "rank": int,         # tier index, 0 = Obscurus ... 11 = Eternus
          "subrank": int,      # 1-6 within the tier (0 for Obscurus)
          "last_match": {      # null while unset (no ranked match yet /
                                # still in placement), otherwise Valve's
                                # rank metadata for that match:
            "match_id": int,
            "start_time": int,
            "player_rank_initial_display_rank": int,
            "player_rank_initial_flat_progress": int | None,
            "player_rank_final_flat_progress": int | None,
            "player_rank_desired_progress_change": int | None,
            "player_rank_initial_calibration_games": int | None,
            "player_rank_initial_demotion_protection_games": int | None,
            "player_rank_consumed_demotion_protection": bool | None,
            "player_rank_initial_win_streak": int | None,
          } | None
        }

    Only ranked matches carry a rank; it stays unset (badge/rank/subrank
    all 0) while the player is in placement games or hasn't finished one.
    """
    try:
        async with AsyncSession(impersonate="chrome") as session:
            url = f"https://api.deadlock-api.com/v1/players/{steamid3}/rank"
            data = await fetch_async_json(url, session)
    except Exception as e:
        print(f"Error fetching player rank for {steamid3}: {e}")
        return None

    if not data:
        return None

    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None

    badge = data.get("badge") or 0
    rank = data.get("rank") or 0
    subrank = data.get("subrank") or 0
    last_match = data.get("last_match")

    return {
        "badge": int(badge),
        "rank": int(rank),
        "subrank": int(subrank),
        "last_match": last_match if isinstance(last_match, dict) else None,
    }


def get_player_rank_image_url(steamid3: str, fmt: str = "webp") -> str:
    """
    Builds the direct URL for GET /v1/players/{account_id}/rank/image.

    This endpoint returns the rank badge image binary directly (with the
    player's I-VI division numeral already drawn on it), so there's no need
    to fetch/cache it server-side -- it can be dropped straight into an
    <img src="..."> tag. Players with no rank yet or still in placement get
    the plain tier badge automatically.
    """
    fmt = "webp" if fmt not in ("png", "webp") else fmt
    return f"https://api.deadlock-api.com/v1/players/{steamid3}/rank/image?format={fmt}"


def calculate_rank_base_pr(badge_num: int) -> float:
    """
    Scales Base PR from 1,000 (Initiate 1) up to 18,500 (Eternus 6).
    Uses exponential curve to create distinct separation at high ranks.
    """
    if badge_num <= 0:
        return 1200.0  # Default unranked baseline

    badge_norm = (badge_num - 1) / 65.0  # 0.0 to 1.0
    base_pr = 1000.0 + (10500.0 * (badge_norm ** 1.35))
    return base_pr


async def get_global_player_metrics() -> dict | None:
    """Fetches global player stat metrics for Elo-like PR baseline comparison."""
    try:
        if METRICS_CACHE_FILE.exists():
            with open(METRICS_CACHE_FILE, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            if time.time() - cached.get('_fetched_at', 0) < METRICS_TTL_SECONDS:
                return cached.get('metrics')
    except Exception as e:
        print(f"Error reading metrics cache: {e}")

    try:
        async with AsyncSession(impersonate="chrome") as session:
            data = await fetch_async_json("https://api.deadlock-api.com/v1/analytics/player-stats/metrics", session)
            print(f"Fetched global metrics")
            if data:
                METRICS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(METRICS_CACHE_FILE, 'w', encoding='utf-8') as f:
                    json.dump({'_fetched_at': time.time(), 'metrics': data}, f)
                return data
    except Exception as e:
        print(f"Error fetching global metrics: {e}")
    return None

# Update the calcPr signature to accept global_metrics
def calcPr(player_stats, steamid3=None, player_rank=None, cached_rank=None, custom_config=None, global_metrics=None, debug=True):
    # =========================================================================
    # 1. TUNING CONFIGURATION & HYPERPARAMETERS
    # =========================================================================
    DEFAULT_CONFIG = {
        "RANK_BLEND_WEIGHT": 0.40,   # 40% rank anchor, 60% stat performance vs Global
        "PERF_WEIGHT_MIN": 0.50,     # Multiplier floor for terrible stats
        "PERF_WEIGHT_MAX": 1.60,     # Multiplier ceiling for elite stats
        "PERF_SENSITIVITY": 1.20,    # Scaling speed of the Z-score curve
        "CONFIDENCE_K": 10.0,        
        "RECENCY_HALF_DAYS": 90.0,   
    }

    cfg = DEFAULT_CONFIG.copy()
    if custom_config and isinstance(custom_config, dict):
        cfg.update(custom_config)

    # We map our internal stat names to the likely metric keys returned by the endpoint
    STAT_MAPPINGS = {
        "kills_per_match":      ("kills", False),     # (metric_key, is_inverted)
        "deaths_per_match":     ("deaths", True),     # Deaths are inverted (fewer is better)
        "assists_per_match":    ("assists", False),
        "damage_per_match":     ("hero_damage", False), 
        "obj_damage_per_match": ("objective_damage", False),
        "networth_per_match":   ("net_worth", False),
    }
    
    # Fallback REFS if the metric endpoint fails or misses a key
    REFS = {
        "kills_per_match":       (4.0, 12.0),
        "deaths_per_match":      (10.0, 3.0), # Inverted fallback
        "assists_per_match":     (3.0, 10.0),
        "damage_per_match":      (10000, 35000),
        "obj_damage_per_match":  (2000, 12000),
        "networth_per_match":    (10000, 35000),
    }

    DEFAULT_WEIGHTS = {
        "kills_per_match":       1.5,
        "deaths_per_match":      2.0,
        "assists_per_match":     1.2,
        "damage_per_match":      1.8,
        "obj_damage_per_match":  1.6,
        "networth_per_match":    1.5,
    }

    now_ts = time.time()
    decay_seconds = cfg["RECENCY_HALF_DAYS"] * 24 * 3600

    def recency_weight(last_played_ts):
        if not last_played_ts: return 0.8
        try:
            age = max(0.0, now_ts - float(last_played_ts))
            return 0.5 ** (age / decay_seconds)
        except Exception: return 0.8

    def match_confidence(matches):
        return matches / (matches + cfg["CONFIDENCE_K"]) if matches > 0 else 0.0

    def calculate_stat_z_score(val, metric_key, is_inverted):
        """Calculates Z-score (standard deviations from average) using global metrics."""
        if global_metrics and metric_key in global_metrics:
            avg = global_metrics[metric_key].get("avg")
            std = global_metrics[metric_key].get("std")
            if avg is not None and std and std > 0:
                z = (val - avg) / std
                return -z if is_inverted else z
                
        # Fallback to linear mapping based on REFS
        poor, great = REFS.get(metric_key, (0, 1))
        if poor == great: return 0.0
        
        # Approximate Z-score range (-2 to +2) from min/max bounds
        if is_inverted:
            norm = (poor - val) / (poor - great)
        else:
            norm = (val - poor) / (great - poor)
        return (max(0.0, min(1.0, norm)) - 0.5) * 4.0 # Map 0..1 to -2..+2

    def calculate_performance_multiplier_from_z(z_score):
        """Maps an average Z-score to a multiplier using a smooth curve (tanh)."""
        # A Z-score of 0 (average) yields 1.0. High positive Z-scores yield PERF_WEIGHT_MAX.
        curved_offset = math.tanh(cfg["PERF_SENSITIVITY"] * z_score * 0.5)
        
        if curved_offset >= 0:
            mult = 1.0 + curved_offset * (cfg["PERF_WEIGHT_MAX"] - 1.0)
        else:
            mult = 1.0 + curved_offset * (1.0 - cfg["PERF_WEIGHT_MIN"])
        return mult

    def z_to_percentile(z_score):
        """Converts a Z-score into a 0-100 percentile via the standard normal
        CDF, i.e. 'you perform better than X% of the reference population'
        for that stat. Same z-scores already computed for the PR blend are
        reused here, so this is free — no extra global data needed."""
        cdf = 0.5 * (1.0 + math.erf(z_score / math.sqrt(2)))
        return max(0.1, min(99.9, round(cdf * 100, 1)))

    # Handle input data structure parsing
    heroes_list = player_stats.get("heroes", [player_stats]) if isinstance(player_stats, dict) else list(player_stats or [])
    
    if player_rank is None:
        if isinstance(player_stats, dict):
            player_rank = player_stats.get("valve_rank_data") or cached_rank or 0
        else:
            player_rank = cached_rank or 0

    badge_num, rank_name, div, tier = resolve_valve_rank(player_rank)
    
    # Calculate base expected rating from rank
    rank_base_pr = calculate_rank_base_pr(badge_num)

    hero_results = []
    total_weight = 0.0
    weighted_pr_sum = 0.0

    if debug:
        print("\n" + "="*80)
        print(f" [DEBUG CALCPR] PLAYER RATING CALCULATION")
        print(f" [VALVE RANK] Resolved Rank: {rank_name} | Badge #: {badge_num} | Base PR: {rank_base_pr:.1f}")
        print("-" * 80)

    for h in heroes_list:
        matches = h.get("matches_played", 0) or 0
        hero_id = h.get("hero_id")
        hero_name = HEROS.get(hero_id, f"Hero {hero_id}")

        if matches <= 0: continue

        damage_match = float(h.get("damage_per_match") or (h.get("total_damage", 0) / max(1, matches)))
        obj_damage_match = float(h.get("obj_damage_per_match") or h.get("objective_damage_per_match") or (h.get("total_objective_damage", 0) / max(1, matches)))
        kills_match = float(h.get("kills_per_match") or (h.get("total_kills", 0) / max(1, matches)))
        deaths_match = float((h.get("total_deaths") or h.get("deaths", 0)) / max(1, matches))
        assists_match = float((h.get("total_assists") or h.get("assists", 0)) / max(1, matches))
        nw_match = float(h.get("networth_per_match") or (h.get("total_networth", 0) / max(1, matches)))

        active_weights = DEFAULT_WEIGHTS.copy()
        if damage_match <= 0: active_weights["damage_per_match"] = 0.0
        if obj_damage_match <= 0: active_weights["obj_damage_per_match"] = 0.0
        
        total_active_weight = sum(active_weights.values())

        raw_stats = {
            "kills_per_match":       (kills_match, f"{kills_match:.1f}"),
            "deaths_per_match":      (deaths_match, f"{deaths_match:.1f}"),
            "assists_per_match":     (assists_match, f"{assists_match:.1f}"),
            "damage_per_match":      (damage_match, f"{damage_match:.0f}" if damage_match > 0 else "N/A"),
            "obj_damage_per_match":  (obj_damage_match, f"{obj_damage_match:.0f}" if obj_damage_match > 0 else "N/A"),
            "networth_per_match":    (nw_match, f"{nw_match:.0f}"),
        }

        # Compute combined Z-Score average
        weighted_z_sum = 0.0
        z_scores_debug = {}
        stat_percentiles = {}

        for stat_key, (val, _) in raw_stats.items():
            if active_weights[stat_key] > 0:
                metric_key, is_inverted = STAT_MAPPINGS[stat_key]
                z_score = calculate_stat_z_score(val, metric_key, is_inverted)
                weighted_z_sum += z_score * active_weights[stat_key]
                z_scores_debug[stat_key] = z_score
                stat_percentiles[stat_key] = z_to_percentile(z_score)

        overall_hero_z = weighted_z_sum / total_active_weight if total_active_weight > 0 else 0.0
        hero_percentile = z_to_percentile(overall_hero_z)
        
        rec_w = recency_weight(h.get("last_played"))
        conf_w = match_confidence(matches)
        weight = conf_w * rec_w

        # Map Z-Score to Multiplier
        perf_mult = calculate_performance_multiplier_from_z(overall_hero_z)
        
        # Rank gives a base expectation, stats determine where they fall against that expectation 
        performance_driven_pr = rank_base_pr * perf_mult
        
        blend_weight = cfg["RANK_BLEND_WEIGHT"]
        
        # Slight PR bump for badge level
        badge_boost = 1.0 + (badge_num / 1000.0) 
        
        hero_pr = ((rank_base_pr * blend_weight) + (performance_driven_pr * (1.0 - blend_weight))) * badge_boost

        weighted_pr_sum += hero_pr * weight
        total_weight += weight

        if debug:
            print(f" [HERO]: {hero_name} (ID: {hero_id}) | Matches: {matches}")
            print(f"    - Avg Z-Score (vs Global) : {overall_hero_z:+.2f}")
            print(f"    - Performance Multiplier  : {perf_mult:.3f}x")
            print(f"    - Final Hero PR           : {hero_pr:.1f}")

        hero_results.append({
            "hero_id":              hero_id,
            "hero_name":            hero_name,
            "matches_played":       matches,
            "overall_z_score":      round(overall_hero_z, 2),
            "percentile":           hero_percentile,
            "stat_percentiles":     stat_percentiles,
            "stat_z_scores":        {k: round(v, 3) for k, v in z_scores_debug.items()},
            "perf_mult":            round(perf_mult, 3),
            "weight":               round(weight, 4),
            "win_rate":             round((h.get("wins", 0) / matches) if matches > 0 else 0.0, 4),
            "hero_pr":              round(hero_pr, 1),
            "total_kills":          int(h.get("total_kills") or h.get("kills") or 0),
            "total_deaths":         int(h.get("total_deaths") or h.get("deaths") or 0),
            "total_assists":        int(h.get("total_assists") or h.get("assists") or 0),
            "total_networth":       int(h.get("total_networth") or h.get("networth") or 0),
            "damage_per_match":     round(damage_match, 1),
            "obj_damage_per_match": round(obj_damage_match, 1),
        })

    overall_pr = (weighted_pr_sum / total_weight) if total_weight > 0 else rank_base_pr

    # Account-level percentile: same recency/confidence weighting used for
    # overall_pr, applied to each hero's z-score - both the overall blend and
    # each individual stat - so the account view is a fair aggregate across
    # everything played, not just your best hero.
    account_stat_keys = list(STAT_MAPPINGS.keys())
    account_weighted_z = 0.0
    account_weighted_stat_z = {k: 0.0 for k in account_stat_keys}
    account_stat_weight = {k: 0.0 for k in account_stat_keys}

    for hr in hero_results:
        w = hr["weight"]
        account_weighted_z += hr["overall_z_score"] * w
        for stat_key, z_val in hr.get("stat_z_scores", {}).items():
            account_weighted_stat_z[stat_key] += z_val * w
            account_stat_weight[stat_key] += w

    overall_percentile = z_to_percentile(account_weighted_z / total_weight) if total_weight > 0 else 50.0
    account_stat_percentiles = {
        stat_key: z_to_percentile(account_weighted_stat_z[stat_key] / account_stat_weight[stat_key])
        for stat_key in account_stat_keys
        if account_stat_weight[stat_key] > 0
    }

    rank_index = int(math.floor((overall_pr - 1000.0) / 269.0)) if overall_pr > 1000.0 else 0
    calculated_badge = max(0, min(65, rank_index)) + 1

    return {
        "overall_pr":              round(overall_pr, 1),
        "overall_percentile":      overall_percentile,
        "stat_percentiles":        account_stat_percentiles,
        "badge":      int(calculated_badge),
        "rank_index": int(rank_index),
        "rank_name":  rank_name,
        "heroes":     sorted(hero_results, key=lambda x: x["weight"], reverse=True),
    }

async def get_latest_patch():
    try:
        async with AsyncSession(impersonate="chrome") as session:
            patches = await fetch_async_json("https://api.deadlock-api.com/v1/patches", session)
            
            if not patches or not isinstance(patches, list):
                return None
                
            latest = patches[0]
            
            try:
                pub_date = datetime.fromisoformat(latest["pub_date"].replace("Z", "+00:00"))
                latest["display_date"] = pub_date.strftime("%B %d, %Y")
            except:
                latest["display_date"] = latest.get("pub_date", "Unknown")
            
            return latest
            
    except Exception as e:
        print(f"Error fetching latest patch: {e}")
        return None


async def get_match_metadata(match_id: int) -> dict | None:
    try:
        async with AsyncSession(impersonate="chrome") as session:
            url = f"https://api.deadlock-api.com/v1/matches/{match_id}/metadata"
            data = await fetch_async_json(url, session)
            return data
    except Exception as e:
        print(f"Match metadata fetch error for {match_id}: {e}")
        return None


def get_hero_background_url(hero_name: str) -> str:
    if not hero_name or hero_name == "Unknown Hero":
        return "/static/images/background.gif"

    formatted_name = hero_name.strip().replace(" ", "_")
    filename = f"{formatted_name}_select_background.png"

    hash_md5 = _hashlib.md5(filename.encode('utf-8')).hexdigest()
    a = hash_md5[0]
    ab = hash_md5[0:2]

    return f"https://deadlock.wiki/images/{a}/{ab}/{filename}"


def get_hero_name_logo_url(hero_name: str) -> str | None:
    if not hero_name or hero_name == "Unknown Hero":
        return None

    formatted_name = hero_name.strip().replace(" ", "_")
    filename = f"{formatted_name}_name.png"

    hash_md5 = _hashlib.md5(filename.encode('utf-8')).hexdigest()
    a = hash_md5[0]
    ab = hash_md5[0:2]

    return f"https://deadlock.wiki/images/{a}/{ab}/{filename}"


# ── Mastery Tab: per-hero deep-dive stats ───────────────────────────────────
#
# Everything below is derived from data this app already collects (match
# history + cached match metadata + item purchases) rather than from any
# "mastery" endpoint on the Deadlock API, since one doesn't exist publicly.

def get_hero_accent_color(hero_name: str) -> str:
    """Deterministically derives a unique accent color per hero (stable across
    requests) so the Mastery tab can re-theme itself per hero without needing
    a hand-authored color for all ~30+ heroes."""
    if not hero_name:
        return "#00d48a"
    digest = int(_hashlib.md5(hero_name.encode("utf-8")).hexdigest(), 16)
    hue = (digest % 360) / 360.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.62, 0.95)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


def _mastery_num(match: dict, *keys):
    for k in keys:
        v = match.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _mastery_record(matches: list, key_fn, reverse: bool = True):
    valid = [m for m in matches if key_fn(m) is not None]
    if not valid:
        return None
    return max(valid, key=key_fn) if reverse else min(valid, key=key_fn)


def build_hero_mastery_stats(match_history: list, hero_id: int, hero_name: str) -> dict | None:
    """
    Builds a 'Mastery' profile for a single hero: first-played date, streaks,
    personal single-match records, and the player's most-bought item build on
    that hero. Only uses matches already present in match_history, so results
    reflect the depth of history this app has fetched (a 'deep' refresh
    unlocks the player's full history for a more accurate first-played date).
    """
    hero_matches = [
        m for m in match_history
        if m.get("hero_id") == hero_id or m.get("hero_name") == hero_name
    ]
    if not hero_matches:
        return None

    chrono = sorted(hero_matches, key=lambda m: int(m.get("start_time") or 0))
    recent_first = list(reversed(chrono))

    first_match = chrono[0]
    first_ts = int(first_match.get("start_time") or 0)
    first_display = first_match.get("date_display") or "Unknown"

    now_ts = time.time()
    days_since_first = round((now_ts - first_ts) / 86400, 1) if first_ts else None

    matches_played = len(hero_matches)
    wins = sum(1 for m in hero_matches if m.get("won"))
    losses = matches_played - wins
    win_rate = (wins / matches_played) if matches_played else 0.0

    # Current streak, based on most recent matches on this hero
    current_streak_count = 0
    current_streak_type = None
    if recent_first:
        current_streak_type = "W" if recent_first[0].get("won") else "L"
        for m in recent_first:
            is_win = bool(m.get("won"))
            if (is_win and current_streak_type == "W") or (not is_win and current_streak_type == "L"):
                current_streak_count += 1
            else:
                break

    longest_win_streak = 0
    longest_loss_streak = 0
    run_w = run_l = 0
    for m in chrono:
        if m.get("won"):
            run_w += 1
            run_l = 0
        else:
            run_l += 1
            run_w = 0
        longest_win_streak = max(longest_win_streak, run_w)
        longest_loss_streak = max(longest_loss_streak, run_l)

    def _ref(m, value, extra=None):
        ref = {
            "match_id": m.get("match_id"),
            "value": value,
            "date_display": m.get("date_display"),
            "won": bool(m.get("won")),
        }
        if extra:
            ref.update(extra)
        return ref

    def _kda(m):
        k = _mastery_num(m, "kills", "player_kills") or 0
        a = _mastery_num(m, "assists", "player_assists") or 0
        d = _mastery_num(m, "deaths", "player_deaths") or 0
        return (k + a) / max(1.0, d)

    records = {}

    m = _mastery_record(hero_matches, lambda x: _mastery_num(x, "kills", "player_kills"))
    if m:
        records["most_kills"] = _ref(m, int(_mastery_num(m, "kills", "player_kills") or 0))

    m = _mastery_record(hero_matches, lambda x: _mastery_num(x, "assists", "player_assists"))
    if m:
        records["most_assists"] = _ref(m, int(_mastery_num(m, "assists", "player_assists") or 0))

    m = _mastery_record(hero_matches, lambda x: _mastery_num(x, "damage", "hero_damage"))
    if m:
        records["most_damage"] = _ref(m, int(_mastery_num(m, "damage", "hero_damage") or 0))

    m = _mastery_record(hero_matches, lambda x: _mastery_num(x, "objective_damage", "obj_damage"))
    if m:
        records["most_objective_damage"] = _ref(m, int(_mastery_num(m, "objective_damage", "obj_damage") or 0))

    m = _mastery_record(hero_matches, lambda x: _mastery_num(x, "net_worth", "networth"))
    if m:
        records["most_networth"] = _ref(m, int(_mastery_num(m, "net_worth", "networth") or 0))

    m = _mastery_record(hero_matches, lambda x: _mastery_num(x, "match_duration_s", "duration_s"))
    if m:
        secs = int(_mastery_num(m, "match_duration_s", "duration_s") or 0)
        records["longest_match"] = _ref(m, f"{secs // 60}m {secs % 60:02d}s")

    m = _mastery_record(hero_matches, lambda x: _mastery_num(x, "match_duration_s", "duration_s"), reverse=False)
    if m:
        secs = int(_mastery_num(m, "match_duration_s", "duration_s") or 0)
        records["shortest_match"] = _ref(m, f"{secs // 60}m {secs % 60:02d}s")

    best_kda_match = _mastery_record(hero_matches, _kda)
    if best_kda_match:
        records["best_kda"] = _ref(best_kda_match, round(_kda(best_kda_match), 2), extra={
            "kills": int(_mastery_num(best_kda_match, "kills", "player_kills") or 0),
            "deaths": int(_mastery_num(best_kda_match, "deaths", "player_deaths") or 0),
            "assists": int(_mastery_num(best_kda_match, "assists", "player_assists") or 0),
        })

    # Signature item build: most frequently *finished* items across every
    # match played on this hero (uses items_info already attached to each
    # match by enrich_match_with_local_data).
    item_counts: dict = {}
    for hm in hero_matches:
        for it in (hm.get("items_info") or []):
            iid = it.get("item_id")
            if iid is None:
                continue
            entry = item_counts.setdefault(iid, {
                "item_id": iid,
                "name": it.get("name"),
                "image_url": it.get("image_url"),
                "count": 0,
            })
            entry["count"] += 1

    signature_items = sorted(item_counts.values(), key=lambda x: x["count"], reverse=True)[:6]
    for it in signature_items:
        it["pick_rate"] = round((it["count"] / matches_played) * 100) if matches_played else 0

    # ── Career Totals ────────────────────────────────────────────────────
    # A "Year in Review"-style cumulative rollup across every tracked match
    # on this hero -- straight sums of stats this app already stores per
    # match, no invented numbers.
    total_kills = 0
    total_assists = 0
    total_deaths = 0
    total_damage = 0.0
    total_objective_damage = 0.0
    total_networth = 0.0
    total_duration_s = 0.0

    for hmatch in hero_matches:
        total_kills += int(_mastery_num(hmatch, "kills", "player_kills") or 0)
        total_assists += int(_mastery_num(hmatch, "assists", "player_assists") or 0)
        total_deaths += int(_mastery_num(hmatch, "deaths", "player_deaths") or 0)
        total_damage += _mastery_num(hmatch, "damage", "hero_damage") or 0
        total_objective_damage += _mastery_num(hmatch, "objective_damage", "obj_damage") or 0
        total_networth += _mastery_num(hmatch, "net_worth", "networth") or 0
        total_duration_s += _mastery_num(hmatch, "match_duration_s", "duration_s") or 0

    total_minutes = (total_duration_s / 60) if total_duration_s else 0

    career_totals = {
        "kills": total_kills,
        "assists": total_assists,
        "deaths": total_deaths,
        "damage": int(total_damage),
        "objective_damage": int(total_objective_damage),
        "souls_earned": int(total_networth),
        "hours_played": round(total_duration_s / 3600, 1),
        "kda": round((total_kills + total_assists) / max(1, total_deaths), 2),
    }

    # ── Playstyle Radar ──────────────────────────────────────────────────
    # A 4-axis fingerprint (Combat / Farm / Objective / Survival) built from
    # this hero's own per-minute rates. Like mastery_score, this is a
    # house-made flavor visualization for readability, not a claim about
    # where the player ranks against the wider playerbase.
    if total_minutes > 0:
        combat_per_min = (total_kills + total_assists) / total_minutes
        farm_per_min = total_networth / total_minutes
        objective_per_min = total_objective_damage / total_minutes
        deaths_per_min = total_deaths / total_minutes
    else:
        combat_per_min = farm_per_min = objective_per_min = deaths_per_min = 0

    def _scale(value, cap):
        return max(0, min(100, round((value / cap) * 100))) if cap else 0

    playstyle_radar = {
        "combat": _scale(combat_per_min, 0.8),
        "farm": _scale(farm_per_min, 35),
        "objective": _scale(objective_per_min, 120),
        "survival": max(0, min(100, round(100 - _scale(deaths_per_min, 0.22)))),
    }

    # Pre-compute the SVG polygon points for the 4-axis radar (N=Combat,
    # E=Farm, S=Objective, W=Survival) around a 200x200 viewBox, center
    # (100,100), so the template can drop it straight into an <svg> with no
    # Jinja-side trigonometry.
    _radar_cx, _radar_cy, _radar_max_r = 100, 100, 80
    _radar_axes = [("combat", 0), ("farm", 90), ("objective", 180), ("survival", 270)]
    _radar_points = []
    for axis_key, axis_angle in _radar_axes:
        r = _radar_max_r * (playstyle_radar[axis_key] / 100)
        rad = math.radians(axis_angle)
        px = round(_radar_cx + r * math.sin(rad), 1)
        py = round(_radar_cy - r * math.cos(rad), 1)
        _radar_points.append((px, py))
    playstyle_radar["svg_polygon"] = " ".join(f"{px},{py}" for px, py in _radar_points)

    return {
        "hero_id": hero_id,
        "hero_name": hero_name,
        "matches_played": matches_played,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate * 100, 1),
        "first_played_ts": first_ts,
        "first_played_display": first_display,
        "days_since_first": days_since_first,
        "current_streak": {"count": current_streak_count, "type": current_streak_type},
        "longest_win_streak": longest_win_streak,
        "longest_loss_streak": longest_loss_streak,
        "records": records,
        "signature_items": signature_items,
        "career_totals": career_totals,
        "playstyle_radar": playstyle_radar,
        "accent_color": get_hero_accent_color(hero_name),
        "bg_image": get_hero_background_url(hero_name),
        "name_logo": get_hero_name_logo_url(hero_name),
    }


# LIVE DATA

async def get_active_matches(limit: int = 25) -> list:
    """
    Fetches active/live matches from https://api.deadlock-api.com/v1/matches/active
    and enriches them with team net worth calculations, momentum, and win probabilities.
    """
    try:
        async with AsyncSession(impersonate="chrome") as session:
            url = "https://api.deadlock-api.com/v1/matches/active"
            data = await fetch_async_json(url, session)
            
            if not data or not isinstance(data, list):
                return []
                
            active_matches = []
            for raw_match in data[:limit]:
                processed = calculate_live_match_prediction(raw_match)
                if processed:
                    active_matches.append(processed)
                    
            # Sort primarily by live spectator count
            return sorted(active_matches, key=lambda x: x.get("spectators", 0), reverse=True)
            
    except Exception as e:
        print(f"Error fetching active matches: {e}")
        return []


def calculate_live_match_prediction(raw_match: dict) -> dict:
    """
    Parses active telemetry matching the /v1/matches/active schema to compute
    win probability %, net worth advantage, momentum status, and roster structure.
    """
    # Primary match/lobby identification
    match_id = raw_match.get("lobby_id") or raw_match.get("match_id") or raw_match.get("id")
    if not match_id:
        return None

    # Spectator Count & Mode Descriptors
    spectators = raw_match.get("spectators") or 0
    match_mode = raw_match.get("match_mode_parsed") or "Unranked"
    game_mode = raw_match.get("game_mode_parsed") or "Standard"

    # Match Clock (Handles duration_s being null during pre-game/lobby setup)
    duration_s = raw_match.get("duration_s")
    if duration_s is None:
        time_display = "Pre-Game"
        duration_s = 0
    else:
        mins = int(duration_s) // 60
        secs = int(duration_s) % 60
        time_display = f"{mins:02d}:{secs:02d}"

    # Team Net Worth values direct from top-level keys
    team0_nw = raw_match.get("net_worth_team_0") or raw_match.get("team0_net_worth") or 0
    team1_nw = raw_match.get("net_worth_team_1") or raw_match.get("team1_net_worth") or 0

    # Team Rosters
    players = raw_match.get("players") or []
    team0_players = []
    team1_players = []

    for p in players:
        team_id = p.get("team") or p.get("player_team") or 0
        hero_id = p.get("hero_id")
        hero_name = HEROS.get(hero_id, "Unknown Hero") if hero_id else "Unknown Hero"
        p_nw = p.get("net_worth", 0)
        
        p_data = {
            "account_id": p.get("account_id"),
            "hero_id": hero_id,
            "hero_name": hero_name,
            "net_worth": p_nw,
            "kills": p.get("kills", 0),
            "deaths": p.get("deaths", 0),
            "assists": p.get("assists", 0),
            "level": p.get("level", 1),
            "abandoned": p.get("abandoned")
        }
        
        if team_id == 0 or str(team_id).lower() in ("amber", "0"):
            team0_players.append(p_data)
        else:
            team1_players.append(p_data)

    # Fallback to summing player net worths if top-level keys were missing/zero
    if team0_nw == 0 and team0_players:
        team0_nw = sum(p["net_worth"] for p in team0_players)
    if team1_nw == 0 and team1_players:
        team1_nw = sum(p["net_worth"] for p in team1_players)

    # Net Worth Delta calculation (Positive = Amber lead, Negative = Sapphire lead)
    nw_delta = team0_nw - team1_nw
    abs_delta = abs(nw_delta)
    
    # Prediction Math: Adjusts sensitivity based on match time
    time_factor = max(0.5, min(2.0, duration_s / 1200)) if duration_s > 0 else 0.5
    scaled_delta = nw_delta / (5000 * time_factor)
    
    amber_win_prob = 1.0 / (1.0 + math.exp(-scaled_delta))
    amber_win_pct = round(amber_win_prob * 100, 1)
    sapphire_win_pct = round(100.0 - amber_win_pct, 1)

    # Momentum Assessment
    if abs_delta < 2000:
        momentum_status = "Even Game"
        leading_team = "Tie"
    elif nw_delta > 0:
        leading_team = "Amber"
        if abs_delta > 15000:
            momentum_status = "Amber Dominating"
        elif abs_delta > 8000:
            momentum_status = "Amber High Momentum"
        else:
            momentum_status = "Amber Slight Lead"
    else:
        leading_team = "Sapphire"
        if abs_delta > 15000:
            momentum_status = "Sapphire Dominating"
        elif abs_delta > 8000:
            momentum_status = "Sapphire High Momentum"
        else:
            momentum_status = "Sapphire Slight Lead"

    return {
        "match_id": match_id,
        "lobby_id": match_id,
        "spectators": spectators,
        "game_time_s": duration_s,
        "time_display": time_display,
        "mode": game_mode,
        "match_mode": match_mode,
        "team0": {
            "name": "Amber",
            "net_worth": team0_nw,
            "win_prediction": amber_win_pct,
            "players": team0_players
        },
        "team1": {
            "name": "Sapphire",
            "net_worth": team1_nw,
            "win_prediction": sapphire_win_pct,
            "players": team1_players
        },
        "lead_team": leading_team,
        "net_worth_delta": nw_delta,
        "abs_net_worth_delta": abs_delta,
        "momentum_status": momentum_status
    }
