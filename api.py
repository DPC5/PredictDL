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
MATCH_DATA_DIR = BASE_DIR / 'data' / 'match_data'

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
        division = int(player_rank.get("division") or player_rank.get("rank_division") or 0)
        tier = int(player_rank.get("division_tier") or player_rank.get("tier") or 0)
        if division > 0:
            badge_num = ((division - 1) * 6) + max(1, min(6, tier if tier > 0 else 1))
        else:
            badge_num = int(player_rank.get("badge") or player_rank.get("rank") or 0)
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


def calculate_rank_base_pr(badge_num: int) -> float:
    """
    Scales Base PR from 1,000 (Initiate 1) up to 18,500 (Eternus 6).
    Uses exponential curve to create distinct separation at high ranks.
    """
    if badge_num <= 0:
        return 1200.0  # Default unranked baseline

    badge_norm = (badge_num - 1) / 65.0  # 0.0 to 1.0
    base_pr = 1000.0 + (17500.0 * (badge_norm ** 1.35))
    return base_pr


def calcPr(player_stats, steamid3=None, player_rank=None, cached_rank=None, custom_config=None, debug=True):
    # =========================================================================
    # 1. TUNING CONFIGURATION & HYPERPARAMETERS
    # =========================================================================
    DEFAULT_CONFIG = {
        "RANK_BLEND_WEIGHT": 0.35,   # 35% rank anchor, 65% stat performance
        "PERF_WEIGHT_MIN": 0.60,     # Multiplier for terrible stats
        "PERF_WEIGHT_MAX": 1.45,     # Multiplier for elite stats
        "PERF_SENSITIVITY": 1.50,    
        "STAT_DIMINISHING_RETURN": 1.2, 
        "CONFIDENCE_K": 10.0,        
        "RECENCY_HALF_DAYS": 90.0,   
    }

    cfg = DEFAULT_CONFIG.copy()
    if custom_config and isinstance(custom_config, dict):
        cfg.update(custom_config)

    # Reference bounds for normalization (Poor, Great)
    REFS = {
        "win_rate":              (0.35,  0.68),
        "kills_per_min":         (0.08,  0.32),
        "deaths_per_min":        (0.22,  0.05), # Inverted
        "assists_per_min":       (0.08,  0.28),
        "networth_per_min":      (250,   1400),
        "accuracy":              (0.25,  0.55),
        "crit_shot_rate":        (0.03,  0.16),
        "ending_level":          (18,    36),
        "damage_per_match":      (6000,  48000),
        "obj_damage_per_match":  (1000,  25000),
        "kills_per_match":       (2.0,   16.0),
        "networth_per_match":    (6000,  60000),
    }

    DEFAULT_WEIGHTS = {
        "win_rate":              2.0,
        "kills_per_min":         1.4,
        "deaths_per_min":        2.0,
        "assists_per_min":       1.2,
        "networth_per_min":      1.5,
        "accuracy":              0.8,
        "crit_shot_rate":        0.7,
        "ending_level":          0.6,
        "damage_per_match":      1.8,
        "obj_damage_per_match":  1.6,
        "kills_per_match":       1.5,
        "networth_per_match":    1.4,
    }

    now_ts = time.time()
    decay_seconds = cfg["RECENCY_HALF_DAYS"] * 24 * 3600

    def recency_weight(last_played_ts):
        if not last_played_ts:
            return 0.8
        try:
            age = max(0.0, now_ts - float(last_played_ts))
            return 0.5 ** (age / decay_seconds)
        except Exception:
            return 0.8

    def match_confidence(matches):
        if matches <= 0:
            return 0.0
        return matches / (matches + cfg["CONFIDENCE_K"])

    def normalized_stat_with_diminishing_returns(val, poor, great, is_inverted=False):
        if poor == great:
            return 0.5
        if is_inverted:
            norm = (poor - val) / (poor - great)
        else:
            norm = (val - poor) / (great - poor)
        norm = max(0.0, norm)
        k = cfg["STAT_DIMINISHING_RETURN"]
        saturated = 1.0 - math.exp(-k * norm)
        saturation_normalizer = 1.0 - math.exp(-k)
        return saturated / saturation_normalizer if saturation_normalizer > 0 else norm

    def calculate_performance_multiplier(raw_score):
        centered_score = raw_score - 0.5
        expanded_score = centered_score * (1.0 + abs(centered_score))
        curved_offset = math.tanh(cfg["PERF_SENSITIVITY"] * expanded_score)
        
        if curved_offset >= 0:
            mult = 1.0 + curved_offset * (cfg["PERF_WEIGHT_MAX"] - 1.0)
        else:
            mult = 1.0 + curved_offset * (1.0 - cfg["PERF_WEIGHT_MIN"])
        return mult

    # Handle input data structure parsing
    if isinstance(player_stats, dict):
        heroes_list = player_stats.get("heroes", [player_stats])
        if player_rank is None:
            player_rank = (
                player_stats.get("rank") 
                or player_stats.get("badge") 
                or player_stats.get("valve_rank_data") 
                or player_stats.get("ranked_mmr") 
                or cached_rank
                or 0
            )
    else:
        heroes_list = list(player_stats or [])
        if player_rank is None:
            player_rank = cached_rank or 0

    badge_num, rank_name, div, tier = resolve_valve_rank(player_rank)
    rank_base_pr = calculate_rank_base_pr(badge_num)

    hero_results = []
    total_weight = 0.0
    weighted_pr_sum = 0.0

    if debug:
        print("\n" + "="*80)
        print(f" [DEBUG CALCPR] PLAYER RATING CALCULATION")
        print("="*80)
        print(f" [VALVE RANK] Resolved Rank: {rank_name} | Badge #: {badge_num} | Base PR: {rank_base_pr:.1f}")
        print("-" * 80)

    for h in heroes_list:
        matches = h.get("matches_played", 0) or 0
        hero_id = h.get("hero_id")
        hero_name = HEROS.get(hero_id, f"Hero {hero_id}")

        if matches <= 0:
            continue

        # Use the player_rank already passed into calcPr
        current_hero_rank = player_rank

        wins = h.get("wins", 0) or 0
        win_rate = wins / matches if matches > 0 else 0.0

        # Raw values extraction
        kpm = float(h.get("kills_per_min") or 0.0)
        dpm = float(h.get("deaths_per_min") or 0.0)
        apm = float(h.get("assists_per_min") or 0.0)
        nwpm = float(h.get("networth_per_min") or 0.0)
        acc = float(h.get("accuracy") or 0.0)
        crit = float(h.get("crit_shot_rate") or 0.0)
        lvl = float(h.get("ending_level") or 0.0)

        damage_match = float(h.get("damage_per_match") or (h.get("total_damage", 0) / max(1, matches)))
        obj_damage_match = float(h.get("obj_damage_per_match") or h.get("objective_damage_per_match") or (h.get("total_objective_damage", 0) / max(1, matches)))
        kills_match = float(h.get("kills_per_match") or (h.get("total_kills", 0) / max(1, matches)))
        nw_match = float(h.get("networth_per_match") or (h.get("total_networth", 0) / max(1, matches)))

        # Handle unindexed matches (where damage/objective damage stats are 0)
        active_weights = DEFAULT_WEIGHTS.copy()
        if damage_match <= 0:
            active_weights["damage_per_match"] = 0.0
        if obj_damage_match <= 0:
            active_weights["obj_damage_per_match"] = 0.0

        total_active_weight = sum(active_weights.values())

        raw_stats = {
            "win_rate":              (win_rate, f"{win_rate*100:.1f}%"),
            "kills_per_min":         (kpm, f"{kpm:.2f}"),
            "deaths_per_min":        (dpm, f"{dpm:.2f}"),
            "assists_per_min":       (apm, f"{apm:.2f}"),
            "networth_per_min":      (nwpm, f"{nwpm:.0f}"),
            "accuracy":              (acc, f"{acc*100:.1f}%"),
            "crit_shot_rate":        (crit, f"{crit*100:.1f}%"),
            "ending_level":          (lvl, f"{lvl:.1f}"),
            "damage_per_match":      (damage_match, f"{damage_match:.0f}" if damage_match > 0 else "N/A (Unindexed)"),
            "obj_damage_per_match":  (obj_damage_match, f"{obj_damage_match:.0f}" if obj_damage_match > 0 else "N/A (Unindexed)"),
            "kills_per_match":       (kills_match, f"{kills_match:.1f}"),
            "networth_per_match":    (nw_match, f"{nw_match:.0f}"),
        }

        # Scaled scores (0.0 to 1.0)
        scaled_scores = {}
        for key in REFS:
            val = raw_stats[key][0]
            poor, great = REFS[key]
            is_inv = (key == "deaths_per_min")
            scaled_scores[key] = normalized_stat_with_diminishing_returns(val, poor, great, is_inverted=is_inv)

        # Weighted score computation
        raw_score = sum(active_weights[k] * scaled_scores[k] for k in active_weights) / total_active_weight

        rec_w = recency_weight(h.get("last_played"))
        conf_w = match_confidence(matches)
        weight = conf_w * rec_w

        perf_mult = calculate_performance_multiplier(raw_score)
        performance_driven_pr = rank_base_pr * perf_mult
        
        blend_weight = cfg["RANK_BLEND_WEIGHT"]
        hero_pr = (rank_base_pr * blend_weight) + (performance_driven_pr * (1.0 - blend_weight))

        weighted_pr_sum += hero_pr * weight
        total_weight += weight

        if debug:
            print(f" [HERO]: {hero_name} (ID: {hero_id}) | Matches: {matches}")
            print(f"  FULL STATS & SCALING:")
            for k, (raw_val, display_str) in raw_stats.items():
                w = active_weights[k]
                sc = scaled_scores[k]
                status = f"[W: {w:.1f}] -> Scaled Score: {sc:.4f}" if w > 0 else "[OMITTED - Missing Data]"
                print(f"    - {k:<22}: Raw = {display_str:<15} {status}")
            
            print(f"  CALC BREAKDOWN:")
            print(f"    - Base Rank PR        : {rank_base_pr:.1f}")
            print(f"    - Raw Performance Score: {raw_score:.4f} / 1.0")
            print(f"    - Performance Multiplier: {perf_mult:.3f}x")
            print(f"    - Perf-Adjusted PR     : {performance_driven_pr:.1f}")
            print(f"    - Final Hero PR        : {hero_pr:.1f} (Confidence/Recency Weight: {weight:.3f})")
            print("-" * 80)

        hero_results.append({
            "hero_id":              hero_id,
            "hero_name":            hero_name,
            "matches_played":       matches,
            "score":                round(raw_score, 4),
            "perf_mult":            round(perf_mult, 3),
            "weight":               round(weight, 4),
            "win_rate":             round(win_rate, 4),
            "hero_pr":              round(hero_pr, 1),
            
            # --- MODIFIED: Exporting Total Stats instead of Per Minute ---
            "total_kills":          int(h.get("total_kills") or h.get("kills") or 0),
            "total_deaths":         int(h.get("total_deaths") or h.get("deaths") or 0),
            "total_assists":        int(h.get("total_assists") or h.get("assists") or 0),
            "total_networth":       int(h.get("total_networth") or h.get("networth") or 0),
            
            "damage_per_match":     round(damage_match, 1),
            "obj_damage_per_match": round(obj_damage_match, 1),
            
            # --- MODIFIED: Accuracy and Headshot Accuracy explicitly declared ---
            "accuracy":             round(acc, 4), 
            "headshot_acc":         round(crit, 4),  # Deadlock API treats headshots as crit_shot_rate
            "crit_shot_rate":       round(crit, 4),  # Preserved in case older templates rely on it
            "ending_level":         round(lvl, 1),
        })

    if total_weight > 0:
        overall_pr = weighted_pr_sum / total_weight
    else:
        overall_pr = rank_base_pr

    # Calculate final badge placement based on overall PR
    rank_index = int(math.floor((overall_pr - 1000.0) / 269.0)) if overall_pr > 1000.0 else 0
    rank_index = max(0, min(65, rank_index))
    calculated_badge = rank_index + 1

    if debug:
        print(f" [SUMMARY RESULT]")
        print(f"  - Final Calculated Overall PR: {overall_pr:.1f}")
        print(f"  - Calculated Badge Tier     : {calculated_badge})")
        print("="*80 + "\n")

    return {
        "overall_pr": round(overall_pr, 1),
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
        "accent_color": get_hero_accent_color(hero_name),
        "bg_image": get_hero_background_url(hero_name),
        "name_logo": get_hero_name_logo_url(hero_name),
    }