"""ESPN public site API: live calendars, scores, DraftKings odds, lineups, standings,
news and tennis draws. Unofficial but key-less; everything is cached."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from ..cache import fetch_json
from ..config import CACHE_TTL
from ..models.common import american_to_decimal

SITE = "https://site.api.espn.com/apis/site/v2/sports"


def _dec(ml):
    return american_to_decimal(ml)


def _parse_soccer_odds(odds_list: list | None) -> dict | None:
    for o in odds_list or []:
        if not isinstance(o, dict):
            continue
        ml = o.get("moneyline") or {}
        mh = (o.get("homeTeamOdds") or {}).get("moneyLine") or (ml.get("home") or {}).get("close", {}).get("odds") or (ml.get("home") or {}).get("open", {}).get("odds")
        ma = (o.get("awayTeamOdds") or {}).get("moneyLine") or (ml.get("away") or {}).get("close", {}).get("odds") or (ml.get("away") or {}).get("open", {}).get("odds")
        md = (o.get("drawOdds") or {}).get("moneyLine") or (ml.get("draw") or {}).get("close", {}).get("odds") or (ml.get("draw") or {}).get("open", {}).get("odds")
        dh, dd, da = _dec(mh), _dec(md), _dec(ma)
        if dh and dd and da:
            return {
                "provider": (o.get("provider") or {}).get("name", "DraftKings"),
                "1x2": [round(dh, 2), round(dd, 2), round(da, 2)],
                "ou_line": o.get("overUnder"),
            }
    return None


def _parse_soccer_event(e: dict) -> dict:
    comp = e["competitions"][0]
    teams = {c["homeAway"]: c for c in comp["competitors"]}
    h, a = teams.get("home", {}), teams.get("away", {})
    st = e["status"]["type"]
    odds = _parse_soccer_odds(comp.get("odds"))
    return {
        "espn_id": e["id"], "kickoff": e["date"], "status": st.get("name"), "state": st.get("state"),
        "status_detail": st.get("shortDetail"),
        "home": h.get("team", {}).get("displayName"), "away": a.get("team", {}).get("displayName"),
        "home_id": h.get("team", {}).get("id"), "away_id": a.get("team", {}).get("id"),
        "home_logo": h.get("team", {}).get("logo"), "away_logo": a.get("team", {}).get("logo"),
        "home_score": h.get("score"), "away_score": a.get("score"),
        "home_form": h.get("form"), "away_form": a.get("form"),
        "home_record": (h.get("records") or [{}])[0].get("summary"), "away_record": (a.get("records") or [{}])[0].get("summary"),
        "venue": (comp.get("venue") or {}).get("fullName"),
        "espn_odds": odds,
    }


def soccer_day(slug: str, d: date) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    ttl = CACHE_TTL["espn_scoreboard"] if d >= today - timedelta(days=1) else 7 * 86400
    j = fetch_json(f"{SITE}/soccer/{slug}/scoreboard?dates={d:%Y%m%d}", ttl)
    return [_parse_soccer_event(e) for e in (j or {}).get("events", [])]


def soccer_fixtures(slug: str, days_back: int = 50, days_ahead: int = 10) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    cur_ym = f"{today:%Y%m}"
    months = sorted({
        f"{(today - timedelta(days=days_back)):%Y%m}",
        f"{(today - timedelta(days=days_back // 2)):%Y%m}",
        cur_ym,
        f"{(today + timedelta(days=days_ahead)):%Y%m}",
    })

    def _fetch_month(ym: str) -> list[dict]:
        ttl = CACHE_TTL["espn_scoreboard"] if ym >= cur_ym else 7 * 86400
        url = f"{SITE}/soccer/{slug}/scoreboard?dates={ym}"
        j = fetch_json(url, ttl) or {}
        evs = j.get("events", [])
        if ym < cur_ym and any((e.get("status") or {}).get("type", {}).get("state") != "post" for e in evs):
            j = fetch_json(url, CACHE_TTL["espn_scoreboard"]) or {}
            evs = j.get("events", [])
        return [_parse_soccer_event(e) for e in evs]

    out, seen = [], set()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for ev_list in pool.map(_fetch_month, months):
            for ev in ev_list:
                if ev["espn_id"] not in seen:
                    seen.add(ev["espn_id"])
                    out.append(ev)

    # Also merge active round from default scoreboard so live scores are fresh
    j_cur = fetch_json(f"{SITE}/soccer/{slug}/scoreboard", CACHE_TTL["espn_scoreboard"]) or {}
    by_id = {ev["espn_id"]: ev for ev in out}
    for e in j_cur.get("events", []):
        ev = _parse_soccer_event(e)
        by_id[ev["espn_id"]] = ev
    return sorted(by_id.values(), key=lambda x: x["kickoff"])


def soccer_recover_odds(slug: str, events: list[dict]) -> None:
    """Recover closing DraftKings odds from summary pickcenter concurrently for events missing odds."""
    need = [ev for ev in events if not ev.get("espn_odds") and ev.get("state") in ("in", "post")]
    if not need:
        return

    def _fetch_one(ev: dict):
        ttl = 30 * 86400 if ev.get("state") == "post" else CACHE_TTL["espn_scoreboard"]
        s = fetch_json(f"{SITE}/soccer/{slug}/summary?event={ev['espn_id']}", ttl) or {}
        ev["espn_odds"] = _parse_soccer_odds(s.get("pickcenter"))

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(_fetch_one, need))


def soccer_summary(slug: str, event_id: str) -> dict:
    """Lineups (starters / subs), injuries listed by ESPN, last five games."""
    j = fetch_json(f"{SITE}/soccer/{slug}/summary?event={event_id}", CACHE_TTL["espn_scoreboard"]) or {}
    lineups = {}
    for r in j.get("rosters") or []:
        side = r.get("homeAway")
        players = r.get("roster") or []
        lineups[side] = {
            "formation": r.get("formation"),
            "starters": [p["athlete"]["displayName"] for p in players if p.get("starter")],
            "subs": [p["athlete"]["displayName"] for p in players if not p.get("starter")],
        }
    injuries = {}
    for blk in j.get("injuries") or []:
        side_team = (blk.get("team") or {}).get("displayName")
        injuries[side_team] = [{"player": (i.get("athlete") or {}).get("displayName"), "status": i.get("status"),
                                "type": (i.get("type") or {}).get("description") if isinstance(i.get("type"), dict) else i.get("type")}
                               for i in blk.get("injuries", [])]
    return {"lineups": lineups, "injuries": injuries}


def news(slug: str, limit: int = 8) -> list[dict]:
    j = fetch_json(f"{SITE}/soccer/{slug}/news", CACHE_TTL["espn_news"]) or {}
    out = []
    for a in (j.get("articles") or [])[:limit]:
        out.append({"headline": a.get("headline"), "description": a.get("description"),
                    "published": a.get("published"),
                    "link": ((a.get("links") or {}).get("web") or {}).get("href")})
    return out


def standings(slug: str) -> list[dict]:
    j = fetch_json(f"https://site.api.espn.com/apis/v2/sports/soccer/{slug}/standings", CACHE_TTL["espn_team"]) or {}
    rows = []
    for child in j.get("children") or []:
        for e in (child.get("standings") or {}).get("entries") or []:
            stats = {s.get("name"): s.get("value") for s in e.get("stats", [])}
            note = (e.get("note") or {}).get("description")
            rows.append({"team": e["team"]["displayName"], "rank": int(stats.get("rank") or 0),
                         "played": int(stats.get("gamesPlayed") or 0), "points": int(stats.get("points") or 0),
                         "gd": int(stats.get("pointDifferential") or 0), "note": note})
    return sorted(rows, key=lambda r: r["rank"])


# ------------------------------------------------------------------ tennis
GRAND_SLAMS = ("australian open", "roland garros", "french open", "wimbledon", "us open")
CHALLENGER_125_KEYWORDS = (
    "rovereto", "valle d'aosta", "curitiba", "queretaro", "cali", "angola", "tolentino",
    "porto", "ankara", "jingshan", "adana", "suzhou", "samsun", "mallorca women",
    "lisboa", "belem", "tampico", "midland", "colina", "florianopolis", "limoges", "angers",
    "challenger", "125",
)


def tennis_rankings(tour: str = "atp") -> dict[str, int]:
    """Current official ATP or WTA singles rankings (top 150) from ESPN."""
    j = fetch_json(f"{SITE}/tennis/{tour.lower()}/rankings", CACHE_TTL["espn_team"]) or {}
    ranks: dict[str, int] = {}
    for grp in j.get("rankings") or []:
        for r in grp.get("ranks") or []:
            name = ((r.get("athlete") or {}).get("displayName") or "").strip()
            cur = r.get("current")
            if name and cur:
                ranks[name.lower()] = int(cur)
    return ranks


def tennis_matches(tour: str = "atp", days_back: int = 35, days_ahead: int = 4) -> list[dict]:
    """Scheduled / live / recent singles matches from ESPN across the last 5 tournament weeks (250+ only)."""
    today = datetime.now(timezone.utc).date()
    d_start = today - timedelta(days=days_back)
    d_end = today + timedelta(days=days_ahead)
    j = fetch_json(f"{SITE}/tennis/{tour}/scoreboard?dates={d_start:%Y%m%d}-{d_end:%Y%m%d}", CACHE_TTL["espn_scoreboard"]) or {}
    by_tourn: dict[str, list[dict]] = {}
    seen = set()
    for e in j.get("events", []):
        tname = e.get("name", "")
        if any(k in tname.lower() for k in CHALLENGER_125_KEYWORDS):
            continue
        slam = any(g in tname.lower() for g in GRAND_SLAMS)
        for g in e.get("groupings", []):
            gname = g.get("grouping", {}).get("displayName", "")
            if "Singles" not in gname:
                continue
            if tour.lower() == "atp" and "Women" in gname:
                continue
            if tour.lower() == "wta" and "Men" in gname:
                continue
            if "Qualifying" in gname:
                continue
            for c in g.get("competitions", []):
                if c["id"] in seen:
                    continue
                rname = (c.get("round") or {}).get("displayName") or ""
                if "Qualifying" in rname:
                    continue
                seen.add(c["id"])
                comps = sorted(c.get("competitors", []), key=lambda x: x.get("order", 0))
                if len(comps) != 2 or not all(x.get("athlete") for x in comps):
                    continue
                st = c["status"]["type"]
                m_item = {
                    "espn_id": c["id"], "tournament": tname, "tour": tour.upper(), "draw": gname,
                    "round": rname, "start": c.get("date"),
                    "status": st.get("name"), "state": st.get("state"), "status_detail": st.get("shortDetail"),
                    "best_of": 5 if (slam and tour == "atp" and "Qualifying" not in rname) else 3,
                    "a": comps[0]["athlete"]["displayName"], "b": comps[1]["athlete"]["displayName"],
                    "a_flag": (comps[0]["athlete"].get("flag") or {}).get("href"),
                    "b_flag": (comps[1]["athlete"].get("flag") or {}).get("href"),
                    "a_seed": comps[0].get("seed"), "b_seed": comps[1].get("seed"),
                    "a_winner": comps[0].get("winner"), "b_winner": comps[1].get("winner"),
                    "score": " ".join(f'{int(x.get("value", 0))}-{int(y.get("value", 0))}'
                                      for x, y in zip(comps[0].get("linescores", []), comps[1].get("linescores", []))),
                }
                by_tourn.setdefault(tname, []).append(m_item)

    # Keep the last 5 completed tournaments + all active/upcoming tournaments per tour
    completed_t, active_t = [], []
    for tname, mlist in by_tourn.items():
        latest_dt = max((m["start"] or "") for m in mlist)
        if all(m["state"] == "post" for m in mlist):
            completed_t.append((latest_dt, mlist))
        else:
            active_t.append((latest_dt, mlist))
    completed_t.sort(key=lambda x: x[0])
    kept = [m for _, mlist in completed_t[-5:] for m in mlist] + [m for _, mlist in active_t for m in mlist]
    return sorted(kept, key=lambda x: x["start"] or "")


# ------------------------------------------------------------------ NBA
def _parse_nba_odds(odds_list: list | None) -> dict | None:
    for o in odds_list or []:
        if not isinstance(o, dict):
            continue
        ml = o.get("moneyline") or {}
        mh = (o.get("homeTeamOdds") or {}).get("moneyLine") or (ml.get("home") or {}).get("close", {}).get("odds") or (ml.get("home") or {}).get("open", {}).get("odds")
        ma = (o.get("awayTeamOdds") or {}).get("moneyLine") or (ml.get("away") or {}).get("close", {}).get("odds") or (ml.get("away") or {}).get("open", {}).get("odds")
        dh = _dec(mh)
        da = _dec(ma)
        if dh and da:
            return {
                "provider": (o.get("provider") or {}).get("name", "DraftKings"),
                "ml": [round(dh, 2), round(da, 2)],
                "ou_line": o.get("overUnder"),
                "spread": o.get("spread"),
                "details": o.get("details"),
            }
    return None


def _extract_leaders(comp_side: dict) -> list[str]:
    names = []
    for cat in comp_side.get("leaders") or []:
        for ldr in cat.get("leaders") or []:
            nm = (ldr.get("athlete") or {}).get("displayName")
            if nm and nm not in names:
                names.append(nm)
    return names


def _parse_nba_summary_absences(s: dict) -> dict[str, list[dict]]:
    """Extract per-team injuries and DNP/benched players from ESPN NBA summary."""
    by_team: dict[str, list[dict]] = {}
    seen_team_player: set[tuple[str, str]] = set()
    for blk in s.get("injuries") or []:
        tname = (blk.get("team") or {}).get("displayName") or ""
        if not tname:
            continue
        for inj in blk.get("injuries") or []:
            p_name = (inj.get("athlete") or {}).get("displayName")
            status = inj.get("status") or "Out"
            detail = (inj.get("details") or {}).get("type") or ""
            if p_name and (tname, p_name) not in seen_team_player:
                seen_team_player.add((tname, p_name))
                by_team.setdefault(tname, []).append({
                    "player": p_name,
                    "status": status,
                    "detail": detail,
                })
    for pblk in (s.get("boxscore") or {}).get("players") or []:
        tname = (pblk.get("team") or {}).get("displayName") or ""
        if not tname:
            continue
        for stat_grp in pblk.get("statistics") or []:
            for ath in stat_grp.get("athletes") or []:
                if ath.get("didNotPlay"):
                    p_name = (ath.get("athlete") or {}).get("displayName")
                    reason = ath.get("reason") or "DNP"
                    if p_name and (tname, p_name) not in seen_team_player:
                        seen_team_player.add((tname, p_name))
                        by_team.setdefault(tname, []).append({
                            "player": p_name,
                            "status": "Out (DNP)",
                            "detail": reason,
                        })
    return by_team


def _parse_nba_event(e: dict) -> dict:
    comp = e["competitions"][0]
    teams = {c["homeAway"]: c for c in comp["competitors"]}
    h, a = teams.get("home", {}), teams.get("away", {})
    st = e["status"]["type"]
    odds = _parse_nba_odds(comp.get("odds"))
    return {
        "espn_id": e["id"],
        "kickoff": e["date"],
        "season_type": (e.get("season") or {}).get("type"),
        "status": st.get("name"),
        "state": st.get("state"),
        "status_detail": st.get("shortDetail"),
        "home": h.get("team", {}).get("displayName"),
        "away": a.get("team", {}).get("displayName"),
        "home_abbr": h.get("team", {}).get("abbreviation"),
        "away_abbr": a.get("team", {}).get("abbreviation"),
        "home_logo": h.get("team", {}).get("logo"),
        "away_logo": a.get("team", {}).get("logo"),
        "home_score": h.get("score"),
        "away_score": a.get("score"),
        "home_record": (h.get("records") or [{}])[0].get("summary"),
        "away_record": (a.get("records") or [{}])[0].get("summary"),
        "home_leaders": _extract_leaders(h),
        "away_leaders": _extract_leaders(a),
        "venue": (comp.get("venue") or {}).get("fullName"),
        "espn_odds": odds,
    }


def nba_injuries() -> dict[str, list[dict]]:
    """Fetch league-wide live NBA injury report across all 30 teams."""
    j = fetch_json(f"{SITE}/basketball/nba/injuries", CACHE_TTL["espn_scoreboard"]) or {}
    out: dict[str, list[dict]] = {}
    for t in j.get("injuries") or []:
        tname = t.get("displayName") or ""
        if not tname:
            continue
        plist = []
        for inj in t.get("injuries") or []:
            p_name = (inj.get("athlete") or {}).get("displayName")
            status = inj.get("status") or "Out"
            detail = (inj.get("details") or {}).get("type") or ""
            if p_name:
                plist.append({"player": p_name, "status": status, "detail": detail})
        if plist:
            out[tname] = plist
    return out


def _parse_nba_pace(s: dict) -> dict[str, float]:
    """Compute possessions per team from ESPN NBA summary boxscore (FGA + 0.44*FTA - ORB + TOV)."""
    paces: dict[str, float] = {}
    for tm in (s.get("boxscore") or {}).get("teams") or []:
        tname = (tm.get("team") or {}).get("displayName") or ""
        st = {x.get("name"): x.get("displayValue") for x in tm.get("statistics") or []}
        try:
            fga = float(str(st.get("fieldGoalsMade-fieldGoalsAttempted") or "0-0").split("-")[1])
            fta = float(str(st.get("freeThrowsMade-freeThrowsAttempted") or "0-0").split("-")[1])
            orb = float(st.get("offensiveRebounds") or 0.0)
            tov = float(st.get("turnovers") or 0.0)
            poss = fga + 0.44 * fta - orb + tov
            if tname and poss > 50.0:
                paces[tname] = round(poss, 1)
        except Exception:
            pass
    return paces


def _nba_day(d: date) -> list[dict]:
    """All NBA events on one scoreboard date (past dates cached for a week once final)."""
    today = datetime.now(timezone.utc).date()
    ttl = CACHE_TTL["espn_scoreboard"] if d >= today - timedelta(days=1) else 7 * 86400
    url = f"{SITE}/basketball/nba/scoreboard?dates={d:%Y%m%d}"
    j = fetch_json(url, ttl) or {}
    evs = j.get("events", [])
    if d < today and any((e.get("status") or {}).get("type", {}).get("state") != "post" for e in evs):
        j = fetch_json(url, CACHE_TTL["espn_scoreboard"]) or {}
        evs = j.get("events", [])
    return [_parse_nba_event(e) for e in evs]


def nba_season_results(season_start: date) -> list[dict]:
    """Every completed regular-season NBA game since season_start (used for point-in-time ratings)."""
    today = datetime.now(timezone.utc).date()
    n = (today - season_start).days
    if n < 0:
        return []
    days = [season_start + timedelta(days=i) for i in range(n + 1)]
    out, seen = [], set()
    with ThreadPoolExecutor(max_workers=10) as pool:
        for ev_list in pool.map(_nba_day, days):
            for ev in ev_list:
                if ev["espn_id"] in seen or ev.get("state") != "post" or ev.get("season_type") not in (2, None):
                    continue
                try:
                    hs, as_ = int(ev["home_score"]), int(ev["away_score"])
                except (TypeError, ValueError):
                    continue
                seen.add(ev["espn_id"])
                out.append({"espn_id": ev["espn_id"], "kickoff": ev["kickoff"], "home": ev["home"],
                            "away": ev["away"], "home_score": hs, "away_score": as_})
    return sorted(out, key=lambda x: x["kickoff"])


def nba_fixtures(days_back: int = 8, days_ahead: int = 7) -> list[dict]:
    """Scheduled, live, and recent NBA games from ESPN scoreboard."""
    today = datetime.now(timezone.utc).date()
    days = [today + timedelta(days=i) for i in range(-days_back, days_ahead + 1)]

    out, seen = [], set()
    with ThreadPoolExecutor(max_workers=10) as pool:
        for ev_list in pool.map(_nba_day, days):
            for ev in ev_list:
                if ev["espn_id"] not in seen:
                    seen.add(ev["espn_id"])
                    out.append(ev)

    # Recover closing DraftKings odds & game-day injuries/DNPs from summary concurrently for finished/live games
    need = [ev for ev in out if ev.get("state") in ("in", "post")]
    if need:
        def _fetch_summary(ev: dict):
            ttl = 30 * 86400 if ev.get("state") == "post" else CACHE_TTL["espn_scoreboard"]
            s = fetch_json(f"{SITE}/basketball/nba/summary?event={ev['espn_id']}", ttl) or {}
            if not ev.get("espn_odds"):
                ev["espn_odds"] = _parse_nba_odds(s.get("pickcenter"))
            ev["espn_absences"] = _parse_nba_summary_absences(s)
            ev["espn_pace"] = _parse_nba_pace(s)

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(_fetch_summary, need))
    return sorted(out, key=lambda x: x["kickoff"])


def nba_standings() -> list[dict]:
    """All 30 NBA teams with PPG for/against, records, splits and streak."""
    j = fetch_json("https://site.api.espn.com/apis/v2/sports/basketball/nba/standings", CACHE_TTL["espn_team"]) or {}
    rows = []
    for child in j.get("children") or []:
        conf = child.get("name", "")
        for e in (child.get("standings") or {}).get("entries") or []:
            st_val = {s.get("name"): s.get("value") for s in e.get("stats", [])}
            st_disp = {s.get("name"): s.get("displayValue") for s in e.get("stats", [])}
            rows.append({
                "team": e["team"]["displayName"],
                "conference": conf,
                "seed": int(st_val.get("playoffSeed") or 0),
                "wins": int(st_val.get("wins") or 0),
                "losses": int(st_val.get("losses") or 0),
                "ppg_for": float(st_val.get("avgPointsFor") or 114.5),
                "ppg_against": float(st_val.get("avgPointsAgainst") or 114.5),
                "diff": float(st_val.get("differential") or 0.0),
                "streak": int(st_val.get("streak") or 0),
                "home_record": st_disp.get("Home") or "0-0",
                "road_record": st_disp.get("Road") or "0-0",
                "last10": st_disp.get("Last Ten Games") or "0-0",
            })
    return rows
