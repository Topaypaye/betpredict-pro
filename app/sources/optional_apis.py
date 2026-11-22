"""Optional paid/free-tier APIs (enabled only if keys are set as env variables).

* ODDS_API_KEY        -> the-odds-api.com : live odds from many EU bookmakers (football + tennis)
* APIFOOTBALL_KEY     -> api-sports.io    : injuries & suspensions per fixture date
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from statistics import mean

from ..cache import CACHE_DIR, fetch_json
from ..config import CACHE_TTL, LEAGUES, ODDS_API_KEY

APIFOOTBALL_KEY = os.environ.get("APIFOOTBALL_KEY", "")
APIFOOTBALL_IDS = {"EPL": 39, "LALIGA": 140, "SERIEA": 135, "BUNDESLIGA": 78, "LIGUE1": 61, "UCL": 2, "UEL": 3, "NATIONS": 5}

WINAMAX_TOURNAMENTS = {
    "EPL": 1,
    "LALIGA": 36,
    "SERIEA": 33,
    "BUNDESLIGA": 42,
    "LIGUE1": 4,
    "UCL": 151665,
    "UEL": 10909,
    "NATIONS": 900002367,
}

# French -> English team name normalization for Winamax.fr
FR_TEAM_MAP = {
    "Paris SG": "Paris Saint-Germain",
    "Lyon": "Olympique Lyonnais",
    "Marseille": "Marseille",
    "Monaco": "AS Monaco",
    "Rennes": "Stade Rennais",
    "Brest": "Stade Brestois",
    "Nice": "OGC Nice",
    "Lille": "Lille",
    "Lens": "RC Lens",
    "Strasbourg": "Strasbourg",
    "Nantes": "Nantes",
    "Reims": "Stade de Reims",
    "Toulouse": "Toulouse",
    "Montpellier": "Montpellier",
    "Auxerre": "AJ Auxerre",
    "Angers": "Angers",
    "Le Havre": "Le Havre",
    "Saint-Etienne": "Saint-Etienne",
    "FC Barcelone": "Barcelona",
    "Espanyol Barcelone": "Espanyol",
    "FC Séville": "Sevilla",
    "Betis Séville": "Real Betis",
    "Atletico Madrid": "Atletico Madrid",
    "Athletic Bilbao": "Athletic Club",
    "Valence": "Valencia",
    "Majorque": "Mallorca",
    "Deportivo La Corogne": "Deportivo La Coruna",
    "AS Rome": "AS Roma",
    "Lazio Rome": "Lazio",
    "Milan AC": "AC Milan",
    "Inter Milan": "Inter",
    "Juventus Turin": "Juventus",
    "Atalanta Bergame": "Atalanta",
    "Naples": "Napoli",
    "Bologne": "Bologna",
    "Côme": "Como",
    "Parme": "Parma",
    "Venise": "Venezia",
    "Gênes": "Genoa",
    "Vérone": "Verona",
    "Hellas Vérone": "Verona",
    "Bayern Munich": "Bayern Munich",
    "Borussia Mönchengladbach": "Gladbach",
    "Eintracht Francfort": "Eintracht Frankfurt",
    "FC Cologne": "FC Koln",
    "Fribourg": "SC Freiburg",
    "Hambourg": "Hamburg",
    "Mayence": "Mainz",
    "Werder Brême": "Werder Bremen",
    "Augsbourg": "Augsburg",
    "VfB Stuttgart": "VfB Stuttgart",
    "Sporting Portugal": "Sporting CP",
    "Club Bruges": "Club Brugge",
    "Étoile Rouge Belgrade": "Crvena Zvezda",
    "Allemagne": "Germany",
    "Angleterre": "England",
    "Espagne": "Spain",
    "Italie": "Italy",
    "Pays-Bas": "Netherlands",
    "Belgique": "Belgium",
    "Suisse": "Switzerland",
    "Suède": "Sweden",
    "Norvège": "Norway",
    "Danemark": "Denmark",
    "Pologne": "Poland",
    "Autriche": "Austria",
    "Croatie": "Croatia",
    "Serbie": "Serbia",
    "Turquie": "Turkey",
    "Grèce": "Greece",
    "Écosse": "Scotland",
    "Pays de Galles": "Wales",
    "République d'Irlande": "Republic of Ireland",
    "Irlande du Nord": "Northern Ireland",
    "République Tchèque": "Czechia",
    "Hongrie": "Hungary",
    "Roumanie": "Romania",
    "Slovaquie": "Slovakia",
    "Slovénie": "Slovenia",
    "Géorgie": "Georgia",
    "Arménie": "Armenia",
    "Islande": "Iceland",
    "Bosnie-Herzégovine": "Bosnia and Herzegovina",
    "Macédoine du Nord": "North Macedonia",
    "Îles Féroé": "Faroe Islands",
}


def _norm_fr_team(name: str) -> str:
    if not name:
        return ""
    s = name.strip()
    return FR_TEAM_MAP.get(s, s)


def _to_stake_odds(prices: list[float], target_overround: float | None = None) -> list[float]:
    """Convert higher-vig regulated odds (e.g. ANJ 7-8% margin) to Stake.com international Betradar margin (~3.8% 2-way, ~4.2% 3-way)."""
    if not prices or any(not isinstance(x, (int, float)) or x <= 1.0 for x in prices):
        return [round(float(x), 2) for x in prices]
    if target_overround is None:
        target_overround = 1.042 if len(prices) == 3 else 1.038
    inv = [1.0 / float(x) for x in prices]
    raw_overround = sum(inv)
    if raw_overround <= target_overround:
        return [round(float(x), 2) for x in prices]
    scale = target_overround / raw_overround
    return [round(max(1.01, 1.0 / (p * scale)), 2) for p in inv]


def _fetch_winamax_state(url: str, ttl: int = 300) -> dict:
    """Fetch and parse Winamax.fr PRELOADED_STATE JSON with local disk caching."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / f"wina_{hashlib.sha1(url.encode()).hexdigest()[:16]}.json"
    if p.exists() and time.time() - p.stat().st_mtime < ttl:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "fr-FR,fr;q=0.9",
        }
        req = urllib.request.Request(url, headers=headers)
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
        if "var PRELOADED_STATE = " not in html:
            return {}
        raw = html.split("var PRELOADED_STATE = ", 1)[1]
        data, _ = json.JSONDecoder().raw_decode(raw)
        # Keep only matches, bets, odds, tournaments to keep cache compact and fast
        slim = {
            "matches": data.get("matches", {}),
            "bets": data.get("bets", {}),
            "odds": data.get("odds", {}),
            "tournaments": data.get("tournaments", {}),
        }
        p.write_text(json.dumps(slim), encoding="utf-8")
        return slim
    except Exception:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}


def winamax_football_odds(league: str | None = None) -> list[dict]:
    """Live French bookmaker (Winamax.fr) 1X2 odds for Football."""
    st = _fetch_winamax_state("https://www.winamax.fr/paris-sportifs/sports/1", CACHE_TTL.get("espn_scoreboard", 300))
    matches = st.get("matches") or {}
    bets = st.get("bets") or {}
    odds_map = st.get("odds") or {}
    target_tid = WINAMAX_TOURNAMENTS.get(league) if league else None
    out = []
    for m in matches.values():
        if not isinstance(m, dict) or m.get("sportId") != 1 or not m.get("mainBetId"):
            continue
        if target_tid and m.get("tournamentId") != target_tid:
            continue
        bid = str(m["mainBetId"])
        bet = bets.get(bid) or {}
        outcomes = bet.get("outcomes") or []
        if len(outcomes) != 3:
            continue
        prices = [odds_map.get(str(o)) for o in outcomes]
        if not all(isinstance(x, (int, float)) and x > 1.0 for x in prices):
            continue
        h = _norm_fr_team(m.get("competitor1Name") or "")
        a = _norm_fr_team(m.get("competitor2Name") or "")
        if h and a:
            vals = _to_stake_odds([float(x) for x in prices], 1.042)
            out.append({
                "home": h,
                "away": a,
                "home_fr": m.get("competitor1Name"),
                "away_fr": m.get("competitor2Name"),
                "provider": "Stake.com",
                "1x2": {"avg": vals, "max": vals, "books": 1},
            })
    return out


def winamax_nba_odds() -> list[dict]:
    """Live French bookmaker (Winamax.fr) moneyline odds for NBA games."""
    st = _fetch_winamax_state("https://www.winamax.fr/paris-sportifs/sports/2/800000076/177", CACHE_TTL.get("espn_scoreboard", 300))
    matches = st.get("matches") or {}
    bets = st.get("bets") or {}
    odds_map = st.get("odds") or {}
    out = []
    for m in matches.values():
        if not isinstance(m, dict) or m.get("tournamentId") != 177 or not m.get("mainBetId"):
            continue
        bid = str(m["mainBetId"])
        bet = bets.get(bid) or {}
        outcomes = bet.get("outcomes") or []
        if len(outcomes) != 2:
            continue
        prices = [odds_map.get(str(o)) for o in outcomes]
        if not all(isinstance(x, (int, float)) and x > 1.0 for x in prices):
            continue
        # Note: On Winamax.fr, Basketball matches are listed as "Home - Away" (competitor1 = Home, competitor2 = Away)
        c1 = (m.get("competitor1Name") or "").strip()
        c2 = (m.get("competitor2Name") or "").strip()
        if c1 and c2:
            vals = _to_stake_odds([float(x) for x in prices], 1.038)
            out.append({
                "home": c1,
                "away": c2,
                "provider": "Stake.com",
                "avg": vals,
                "max": vals,
                "ou_line": None,
                "spread": None,
            })
    return out


def winamax_tennis_odds() -> list[dict]:
    """Live French bookmaker (Winamax.fr) moneyline odds for ATP & WTA Tennis."""
    st = _fetch_winamax_state("https://www.winamax.fr/paris-sportifs/sports/5", CACHE_TTL.get("espn_scoreboard", 300))
    matches = st.get("matches") or {}
    bets = st.get("bets") or {}
    odds_map = st.get("odds") or {}
    out = []
    for m in matches.values():
        if not isinstance(m, dict) or m.get("sportId") != 5 or not m.get("mainBetId"):
            continue
        bid = str(m["mainBetId"])
        bet = bets.get(bid) or {}
        outcomes = bet.get("outcomes") or []
        if len(outcomes) != 2:
            continue
        prices = [odds_map.get(str(o)) for o in outcomes]
        if not all(isinstance(x, (int, float)) and x > 1.0 for x in prices):
            continue
        p1 = (m.get("competitor1Name") or "").strip()
        p2 = (m.get("competitor2Name") or "").strip()
        if p1 and p2 and "/" not in p1 and "/" not in p2:
            vals = _to_stake_odds([float(x) for x in prices], 1.038)
            out.append({
                "a": p1,
                "b": p2,
                "provider": "Stake.com",
                "avg": vals,
                "max": vals,
            })
    return out


def _summ(prices: list[list[float]]):
    cols = list(zip(*prices))
    return {"avg": [round(mean(c), 3) for c in cols], "max": [max(c) for c in cols], "books": len(prices)}


def odds_api_football(league: str) -> list[dict]:
    out = winamax_football_odds(league)
    if not ODDS_API_KEY:
        return out
    key = LEAGUES[league]["odds_api"]
    url = (f"https://api.the-odds-api.com/v4/sports/{key}/odds?apiKey={ODDS_API_KEY}"
           f"&regions=eu,uk,fr&markets=h2h,totals&oddsFormat=decimal")
    data = fetch_json(url, CACHE_TTL["odds_api"]) or []
    for ev in data if isinstance(data, list) else []:
        h2h, ou = [], []
        for bm in ev.get("bookmakers", []):
            for m in bm.get("markets", []):
                oc = {o["name"]: o for o in m.get("outcomes", [])}
                if m["key"] == "h2h" and {ev["home_team"], ev["away_team"], "Draw"} <= set(oc):
                    h2h.append([oc[ev["home_team"]]["price"], oc["Draw"]["price"], oc[ev["away_team"]]["price"]])
                if m["key"] == "totals" and "Over" in oc and abs(oc["Over"].get("point", 0) - 2.5) < 1e-6:
                    ou.append([oc["Over"]["price"], oc["Under"]["price"]])
        if h2h:
            out.append({"home": ev["home_team"], "away": ev["away_team"], "kickoff": ev["commence_time"],
                        "provider": "the-odds-api", "1x2": _summ(h2h), "ou25": _summ(ou) if ou else None})
    return out


def odds_api_tennis() -> list[dict]:
    if not ODDS_API_KEY:
        return []
    sports = fetch_json(f"https://api.the-odds-api.com/v4/sports?apiKey={ODDS_API_KEY}", 6 * 3600) or []
    keys = [s["key"] for s in sports if isinstance(s, dict) and s.get("group") == "Tennis" and s.get("active")]
    out = []
    for k in keys[:6]:
        data = fetch_json(f"https://api.the-odds-api.com/v4/sports/{k}/odds?apiKey={ODDS_API_KEY}"
                          f"&regions=eu,uk&markets=h2h&oddsFormat=decimal", CACHE_TTL["odds_api"]) or []
        for ev in data if isinstance(data, list) else []:
            prices = []
            for bm in ev.get("bookmakers", []):
                for m in bm.get("markets", []):
                    oc = {o["name"]: o["price"] for o in m.get("outcomes", [])}
                    if ev["home_team"] in oc and ev["away_team"] in oc:
                        prices.append([oc[ev["home_team"]], oc[ev["away_team"]]])
            if prices:
                out.append({"a": ev["home_team"], "b": ev["away_team"], "start": ev["commence_time"], **_summ(prices)})
    return out


def bovada_tennis_odds() -> list[dict]:
    """Free public live moneyline odds for active ATP and WTA events."""
    data = fetch_json(
        "https://www.bovada.lv/services/sports/event/v2/events/A/description/tennis",
        CACHE_TTL.get("espn_scoreboard", 300),
    ) or []
    out = []
    for group in data if isinstance(data, list) else []:
        for ev in group.get("events", []):
            desc = ev.get("description", "")
            if " vs " not in desc or " / " in desc:
                continue
            parts = desc.split(" vs ")
            if len(parts) != 2:
                continue
            p1, p2 = parts[0].strip(), parts[1].strip()
            markets = ev.get("displayGroups", [{}])[0].get("markets", [])
            ml = [m for m in markets if m.get("description") == "Moneyline" or m.get("key") == "2way-moneyline"]
            if not ml:
                continue
            outcomes = ml[0].get("outcomes", [])
            if len(outcomes) != 2:
                continue
            o_dict = {}
            for o in outcomes:
                try:
                    price = float(o.get("price", {}).get("decimal", 0))
                    if price > 1.0:
                        o_dict[o.get("description", "").strip()] = round(price, 2)
                except (ValueError, TypeError):
                    pass
            if p1 in o_dict and p2 in o_dict:
                out.append({
                    "a": p1,
                    "b": p2,
                    "provider": "Bovada",
                    "avg": [o_dict[p1], o_dict[p2]],
                    "max": [o_dict[p1], o_dict[p2]],
                })
    return out


def live_tennis_odds() -> list[dict]:
    """Aggregates French Winamax.fr odds, paid The-Odds-API (if key provided), and Bovada fallback."""
    odds = winamax_tennis_odds()
    if ODDS_API_KEY:
        odds.extend(odds_api_tennis())
    odds.extend(bovada_tennis_odds())
    return odds


def bovada_nba_odds() -> list[dict]:
    """Free public live moneyline, spread, and O/U total odds for NBA games."""
    data = fetch_json(
        "https://www.bovada.lv/services/sports/event/v2/events/A/description/basketball/nba",
        CACHE_TTL.get("espn_scoreboard", 300),
    ) or []
    out = []
    for group in data if isinstance(data, list) else []:
        for ev in group.get("events", []):
            desc = ev.get("description", "")
            if " @ " in desc:
                away, home = [p.strip() for p in desc.split(" @ ", 1)]
            elif " vs " in desc:
                home, away = [p.strip() for p in desc.split(" vs ", 1)]
            else:
                continue
            markets = ev.get("displayGroups", [{}])[0].get("markets", [])
            ml_dict = {}
            ou_line = None
            spread_line = None
            for m in markets:
                m_desc = m.get("description", "")
                m_key = m.get("key", "")
                outcomes = m.get("outcomes", [])
                # Full-game moneyline (exclude 1H / 1Q)
                if (m_desc == "Moneyline" or m_key == "2W-12") and not ml_dict:
                    for o in outcomes:
                        name = o.get("description", "").strip()
                        if " - " in name:
                            continue
                        try:
                            pr = float(o.get("price", {}).get("decimal", 0))
                            if pr > 1.0:
                                ml_dict[name] = round(pr, 2)
                        except (ValueError, TypeError):
                            pass
                elif (m_desc == "Total" or m_key == "2W-OU") and ou_line is None:
                    for o in outcomes:
                        if " - " in o.get("description", ""):
                            continue
                        try:
                            ou_line = float(o.get("price", {}).get("handicap", 0))
                            break
                        except (ValueError, TypeError):
                            pass
                elif (m_desc == "Point Spread" or m_key == "2W-HCAP") and spread_line is None:
                    for o in outcomes:
                        if o.get("description", "").strip() == home:
                            try:
                                spread_line = float(o.get("price", {}).get("handicap", 0))
                            except (ValueError, TypeError):
                                pass
            if home in ml_dict and away in ml_dict:
                out.append({
                    "home": home,
                    "away": away,
                    "provider": "Bovada",
                    "avg": [ml_dict[home], ml_dict[away]],
                    "max": [ml_dict[home], ml_dict[away]],
                    "ou_line": ou_line,
                    "spread": spread_line,
                })
    return out


def live_nba_odds() -> list[dict]:
    """Live NBA moneyline, spread & total odds (French Winamax.fr primary + Bovada fallback)."""
    return winamax_nba_odds() + bovada_nba_odds()


def injuries(league: str, day: str, season: int) -> list[dict]:
    """[{team, player, type, reason}] for fixtures of `league` on `day` (YYYY-MM-DD)."""
    if not APIFOOTBALL_KEY or league not in APIFOOTBALL_IDS:
        return []
    import requests
    from ..cache import CACHE_DIR
    import hashlib, json, time
    url = f"https://v3.football.api-sports.io/injuries?league={APIFOOTBALL_IDS[league]}&season={season}&date={day}"
    p = CACHE_DIR / f"apif_{hashlib.sha1(url.encode()).hexdigest()[:16]}.json"
    if p.exists() and time.time() - p.stat().st_mtime < 6 * 3600:
        data = json.loads(p.read_text())
    else:
        try:
            r = requests.get(url, headers={"x-apisports-key": APIFOOTBALL_KEY}, timeout=20)
            data = r.json()
            p.write_text(json.dumps(data))
        except Exception:  # noqa: BLE001
            data = json.loads(p.read_text()) if p.exists() else {}
    return [{"team": x["team"]["name"], "player": x["player"]["name"], "type": x["player"].get("type"),
             "reason": x["player"].get("reason")} for x in (data.get("response") or [])]


SPORTSGAMBLER_SLUGS = {
    "LALIGA": "spain-la-liga",
    "SERIEA": "italy-serie-a",
    "BUNDESLIGA": "germany-bundesliga",
    "LIGUE1": "france-ligue-1",
    "UCL": "uefa-champions-league",
    "UEL": "uefa-europa-league",
}


def _fpl_epl_injuries() -> dict[str, list[dict]]:
    """Fetch live EPL injuries & suspensions from the official Fantasy Premier League API."""
    p = CACHE_DIR / "fpl_epl_injuries.json"
    if p.exists() and time.time() - p.stat().st_mtime < 4 * 3600:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        req = urllib.request.Request(
            "https://fantasy.premierleague.com/api/bootstrap-static/",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            j = json.loads(r.read().decode("utf-8"))
    except Exception:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    teams = {t["id"]: t["name"] for t in j.get("teams", [])}
    out: dict[str, list[dict]] = {}
    for el in j.get("elements", []):
        st = el.get("status")
        if st not in ("i", "s", "d"):
            continue
        tname = teams.get(el.get("team"))
        if not tname:
            continue
        mins = int(el.get("minutes") or 0)
        ga = int(el.get("goals_scored") or 0) + int(el.get("assists") or 0)
        sel_pct = float(el.get("selected_by_percent") or 0.0)
        cost = float(el.get("now_cost") or 0.0)
        is_star = mins >= 500 or ga >= 3 or sel_pct >= 8.0 or cost >= 75.0
        status_label = "Injured" if st == "i" else ("Suspended" if st == "s" else "Doubtful")
        if st in ("i", "s"):
            weight = 1.0 if (is_star or mins >= 350 or ga >= 2) else (0.4 if mins >= 90 else 0.0)
        else:
            weight = 0.5 if (is_star or mins >= 350 or ga >= 2) else 0.0
        out.setdefault(tname, []).append({
            "player": f"{el.get('first_name', '')} {el.get('second_name', '')}".strip() or el.get("web_name", ""),
            "status": status_label,
            "reason": el.get("news") or "",
            "minutes": mins,
            "weight": weight,
            "is_star": is_star,
        })
    for tname in out:
        out[tname].sort(key=lambda x: (not x.get("is_star"), -(x.get("weight") or 0.0), -(x.get("minutes") or 0)))
    try:
        p.write_text(json.dumps(out), encoding="utf-8")
    except Exception:
        pass
    return out


def _sportsgambler_injuries(league: str) -> dict[str, list[dict]]:
    """Fetch live European football injuries & suspensions from SportsGambler."""
    import html as _html
    import re

    slug = SPORTSGAMBLER_SLUGS.get(league)
    if not slug:
        return {}
    p = CACHE_DIR / f"sg_inj_{league.lower()}.json"
    if p.exists() and time.time() - p.stat().st_mtime < 4 * 3600:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    url = f"https://www.sportsgambler.com/injuries/football/{slug}/"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            raw_html = r.read().decode("utf-8", errors="ignore")
    except Exception:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    out: dict[str, list[dict]] = {}
    blocks = raw_html.split('<div class="injury-block">')[1:]
    row_re = re.compile(
        r'<span class="inj-type\s+([^"]*)">.*?</span>\s*'
        r'<span class="inj-player">(.*?)</span>\s*'
        r'<span class="inj-position[^"]*">(.*?)</span>\s*'
        r'<span class="inj-game[^"]*">(.*?)</span>\s*'
        r'<span class="inj-goals[^"]*">(.*?)</span>\s*'
        r'<span class="inj-assist[^"]*">(.*?)</span>\s*'
        r'<span class="inj-info">(.*?)</span>',
        re.S,
    )
    for blk in blocks:
        m_team = re.search(r'<h3 class="injuries-title"[^>]*>\s*<a[^>]*>(.*?)</a>', blk, re.S)
        if not m_team:
            m_team = re.search(r'<h3 class="injuries-title"[^>]*>(.*?)</h3>', blk, re.S)
        if not m_team:
            continue
        tname = _html.unescape(re.sub(r"<[^>]+>", "", m_team.group(1))).strip()
        if not tname or "bet now" in tname.lower() or len(tname) > 45:
            continue
        for rm in row_re.finditer(blk):
            cls_type, pl, pos, gms, gls, ast, info = [_html.unescape(re.sub(r"<[^>]+>", "", g)).strip() for g in rm.groups()]
            if not pl:
                continue
            is_susp = "card" in cls_type or "suspend" in info.lower()
            is_doubt = "question" in cls_type
            status_label = "Suspended" if is_susp else ("Doubtful" if is_doubt else "Injured")
            n_games = int(gms) if gms.isdigit() else 0
            n_ga = (int(gls) if gls.isdigit() else 0) + (int(ast) if ast.isdigit() else 0)
            is_star = n_games >= 6 or n_ga >= 3
            if is_doubt:
                weight = 0.5 if (n_games >= 4 or n_ga >= 2) else 0.0
            else:
                weight = 1.0 if (n_games >= 4 or n_ga >= 2) else (0.4 if n_games >= 1 else 0.0)
            out.setdefault(tname, []).append({
                "player": pl,
                "status": status_label,
                "reason": info,
                "matches": n_games,
                "weight": weight,
                "is_star": is_star,
            })
    for tname in out:
        out[tname].sort(key=lambda x: (not x.get("is_star"), -(x.get("weight") or 0.0), -(x.get("matches") or 0)))
    try:
        p.write_text(json.dumps(out), encoding="utf-8")
    except Exception:
        pass
    return out


def football_injuries(league: str) -> dict[str, list[dict]]:
    """Live per-team injury & suspension dict for any Football league (no paid API key required)."""
    if league == "EPL":
        return _fpl_epl_injuries()
    if league in SPORTSGAMBLER_SLUGS:
        return _sportsgambler_injuries(league)
    return {}
