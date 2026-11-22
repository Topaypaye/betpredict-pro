"""Orchestration: builds engines from history, merges live fixtures + odds,
produces explainable predictions and recommendations, keeps a live track record."""
from __future__ import annotations

import difflib
import gc
import json
import threading
import time
from datetime import date, datetime, timezone

import pandas as pd

from . import config as C
from .models.common import devig, kelly
from .models.football import COMPONENTS as FB_COMPONENTS, FootballEngine, blend as fb_blend
from .models.nba import COMPONENTS as NBA_COMPONENTS, NBA_BASE_ELO, NBAEngine, blend as nba_blend, standings_as_of
from .models.tennis import COMPONENTS as TN_COMPONENTS, TennisEngine, blend as tn_blend
from .names import match_player, match_team
from .sources import espn, footballdata, optional_apis, tennisdata

SETTINGS_FILE = C.BASE_DIR / "data" / "settings.json"
TRACK_FILE = C.BASE_DIR / "data" / "track_record.json"
_lock = threading.RLock()

LABELS_1X2 = ["Home win", "Draw", "Away win"]

COMPONENT_INFO = {
    "football": {
        "market": "Bookmaker consensus: average decimal odds across bookmakers, margin removed (1/odds normalised).",
        "xg_poisson": "Expected goals (xG) for/against, opponent- and venue-adjusted, exponentially weighted (~12 matches), turned into a Dixon-Coles Poisson score grid.",
        "goals_poisson": "Same Poisson model on real goals scored/conceded (captures finishing quality and luck).",
        "elo": "Elo rating (K=24, goal-margin multiplier, home advantage 45 pts, 20% regression between seasons, promoted teams start 90 pts below league mean).",
        "form": "Last 5 league games: points per game and xG difference per game.",
        "table": "Season-to-date points per game and goal difference, shrunk toward league average in the first weeks.",
        "h2h": "Last 6 head-to-head meetings (5 years), shrunk toward league base rates (6 pseudo-matches).",
    },
    "tennis": {
        "market": "Bookmaker consensus odds, margin removed.",
        "surface_elo": "50/50 blend of overall Elo and surface-specific Elo (Hard / Clay / Grass).",
        "elo": "Overall Elo with experience-dependent K factor (FiveThirtyEight method).",
        "ranking": "Official ranking: logistic on log(rank ratio).",
        "form": "Win rate over last 120 days, shrunk toward 50%.",
        "h2h": "Head-to-head record, shrunk toward 50% (3 pseudo-matches).",
    },
    "nba": {
        "market": "Bookmaker consensus moneyline odds (DraftKings / Bovada), margin removed.",
        "elo": "FiveThirtyEight NBA Elo rating with Margin-of-Victory multiplier and +70 Elo home-court advantage.",
        "net_rating": "Offensive & Defensive PPG efficiency shrunk toward franchise priors, mapped to expected point margin.",
        "pythagorean": "Morey's Basketball Pythagorean expectation (exponent 13.91) combined via Log5.",
        "form": "Last-10 games momentum and active win/loss streak.",
        "home_road": "Home court vs Road court win-rate efficiency split.",
    },
}


# ------------------------------------------------------------------ settings
DEFAULT_SETTINGS = {
    "football_market_share": C.FOOTBALL_MARKET_SHARE,
    "tennis_market_share": C.TENNIS_MARKET_SHARE,
    "nba_market_share": C.NBA_MARKET_SHARE,
    "football_stats_weights": None,     # None -> tuned weights file -> config defaults
    "tennis_stats_weights": None,
    "nba_stats_weights": None,
    "value_edge_min": C.RECO["value_edge_min"],
    "manual_absences": {},              # espn_id -> {"home": n, "away": n}
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text()))
        except ValueError:
            pass
    return s


def save_settings(upd: dict) -> dict:
    with _lock:
        s = load_settings()
        for k, v in upd.items():
            if k in DEFAULT_SETTINGS:
                s[k] = v
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(s, indent=1))
        invalidate_all_caches()
        return s


def stats_weights(sport: str) -> dict:
    s = load_settings()
    if s.get(f"{sport}_stats_weights"):
        return s[f"{sport}_stats_weights"]
    if C.WEIGHTS_FILE.exists():
        try:
            w = json.loads(C.WEIGHTS_FILE.read_text()).get(sport, {}).get("stats")
            if w:
                return w
        except ValueError:
            pass
    if sport == "football":
        return C.FOOTBALL_STATS_WEIGHTS
    if sport == "nba":
        return C.NBA_STATS_WEIGHTS
    return C.TENNIS_STATS_WEIGHTS


def weights(sport: str) -> dict:
    s = load_settings()
    return C.effective_weights(stats_weights(sport), float(s[f"{sport}_market_share"]))


# ------------------------------------------------------------------ track record
def _track_load() -> dict:
    if TRACK_FILE.exists():
        try:
            return json.loads(TRACK_FILE.read_text())
        except ValueError:
            return {}
    return {}


def _track_save(d: dict) -> None:
    TRACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRACK_FILE.write_text(json.dumps(d, indent=0))


# ------------------------------------------------------------------ recommendation
def recommend(p: list[float], labels: list[str], best_odds: list | None, avg_odds: list | None,
              p_stats: list[float] | None, p_market: list[float] | None) -> dict:
    R = C.RECO
    s = load_settings()
    edge_min = float(s.get("value_edge_min", R["value_edge_min"]))
    i = max(range(len(p)), key=lambda k: p[k])
    if len(p) == 2:
        # 2-way sports (NBA / Tennis) have a 50% baseline; <56% is Low, 56-65% is Medium, >=65% is High
        conf = "High" if p[i] >= 0.65 else ("Medium" if p[i] >= 0.56 else "Low")
    else:
        conf = "High" if p[i] >= R["conf_high"] else ("Medium" if p[i] >= R["conf_medium"] else "Low")
    out = {"pick": labels[i], "pick_index": i, "probability": round(p[i], 4), "confidence": conf,
           "fair_odds": [round(1 / x, 2) if x > 0 else None for x in p]}
    if len(p) == 3 and p[i] < R["conf_medium"]:
        dc = {"1X (home or draw)": p[0] + p[1], "X2 (draw or away)": p[1] + p[2], "12 (no draw)": p[0] + p[2]}
        k = max(dc, key=dc.get)
        out["double_chance"] = {"market": k, "probability": round(dc[k], 4)}
    values = []
    for k in range(len(p)):
        o = (best_odds or [None] * len(p))[k]
        if o and o > 1 and p[k] >= R["value_prob_min"]:
            edge = p[k] * o - 1
            if edge >= edge_min:
                values.append({"outcome": labels[k], "odds": round(o, 2), "model_prob": round(p[k], 4),
                               "edge": round(edge, 4),
                               "kelly_stake_pct": round(100 * kelly(p[k], o, R["kelly_fraction"], R["kelly_cap"]), 2)})
    out["value_bets"] = sorted(values, key=lambda v: -v["edge"])
    if p_stats and p_market:
        si = max(range(len(p_stats)), key=lambda k: p_stats[k])
        mi = max(range(len(p_market)), key=lambda k: p_market[k])
        agree = (si == mi)
        if not agree:
            out["confidence"] = "Medium" if out["confidence"] == "High" else "Low"
        out["stats_vs_market"] = {"agree": agree, "stats_pick": labels[si], "market_pick": labels[mi],
                                  "max_gap_pp": round(100 * max(abs(a - b) for a, b in zip(p_stats, p_market)), 1)}
    return out


# ================================================================== FOOTBALL
class FootballService:
    def __init__(self):
        self._lock = threading.RLock()
        self.engine: FootballEngine | None = None
        self.meta: dict = {}
        self.built_at = 0.0
        self.history: pd.DataFrame | None = None
        self.prematch_preds: dict[tuple[str, str, str], dict] = {}
        self._fixtures_cache: dict[str, tuple[float, list[dict]]] = {}

    def ensure(self, max_age: int = 3 * 3600):
        with self._lock:
            if self.engine is None or time.time() - self.built_at > max_age:
                df, meta = footballdata.load_all()
                eng = FootballEngine()
                prematch = {}

                def _cb(row, pred):
                    prematch[(row.league, row.home, row.away)] = pred

                eng.run(df, callback=_cb, callback_season=C.CURRENT_SEASON)
                cur_df = df[df.season == C.CURRENT_SEASON].copy()
                del df
                gc.collect()
                self.engine, self.meta, self.history, self.prematch_preds, self.built_at = (
                    eng, meta, cur_df, prematch, time.time()
                )
        return self.engine

    def _fd_odds_lookup(self):
        lk = {}
        if self.history is not None:
            # Include completed season matches with full bookmaker odds
            cur_season = self.history[self.history.season == C.CURRENT_SEASON]
            for r in cur_season.itertuples(index=False):
                lk[(r.league, r.home, r.away)] = r
        fx = footballdata.load_fixtures()
        for r in fx.itertuples(index=False):
            lk[(r.league, r.home, r.away)] = r
        return lk

    def fixtures(self, league: str, with_finished: bool = True, force_refresh: bool = False) -> list[dict]:
        with _lock:
            cached = self._fixtures_cache.get(league)
            if not force_refresh and cached and (time.time() - cached[0] < 600):
                all_fix = cached[1]
                return [x for x in all_fix if x.get("state") != "post"] if not with_finished else all_fix

        eng = self.ensure()
        lg = C.LEAGUES[league]
        if league in ("UCL", "UEL", "NATIONS"):
            cands = sorted(set(k[1] for k in eng.teams.keys()))
        else:
            cands = eng.teams_in(league, C.CURRENT_SEASON)
        fd = self._fd_odds_lookup()
        oapi = optional_apis.odds_api_football(league)
        fb_inj_map = optional_apis.football_injuries(league)
        fb_inj_teams = list(fb_inj_map.keys())
        settings = load_settings()
        W = weights("football")
        track = _track_load()

        raw_evs = espn.soccer_fixtures(lg["espn"])
        if not raw_evs:
            return []

        # Cluster fixtures into matchweeks / tournament rounds based on kickoff dates
        raw_evs.sort(key=lambda x: x["kickoff"])
        clusters = []
        cur = [0]
        d_start = pd.Timestamp(raw_evs[0]["kickoff"]).date()
        for i in range(1, len(raw_evs)):
            d_prev = pd.Timestamp(raw_evs[i - 1]["kickoff"]).date()
            d_curr = pd.Timestamp(raw_evs[i]["kickoff"]).date()
            if (d_curr - d_prev).days > 2 or (d_curr - d_start).days >= 4:
                clusters.append(cur)
                cur = [i]
                d_start = d_curr
            else:
                cur.append(i)
        clusters.append(cur)

        first_active_idx = next(
            (ci for ci, c in enumerate(clusters) if any(raw_evs[j]["state"] != "post" for j in c)),
            len(clusters),
        )
        base_played = max(
            (eng.teams[(league, t)].s_games for t in cands if (league, t) in eng.teams),
            default=0,
        )
        # Also check ESPN W-D-L record on upcoming cluster if available
        if first_active_idx < len(clusters):
            rec_totals = []
            for j in clusters[first_active_idx]:
                for rk in ("home_record", "away_record"):
                    rec_str = raw_evs[j].get(rk) or ""
                    parts = [int(p) for p in rec_str.split("-") if p.isdigit()]
                    if len(parts) == 3:
                        rec_totals.append(sum(parts))
            if rec_totals:
                base_played = max(base_played, int(pd.Series(rec_totals).mode().iloc[0]))

        for ci, c in enumerate(clusters):
            c_items = [raw_evs[j] for j in c]
            d0 = pd.Timestamp(c_items[0]["kickoff"]).strftime("%b %d")
            d1 = pd.Timestamp(c_items[-1]["kickoff"]).strftime("%b %d")
            d_span = d0 if d0 == d1 else f"{d0} – {d1}"
            offset = ci - first_active_idx

            if league in ("UCL", "UEL"):
                round_num = ci + 1
                r_name = f"Matchday {round_num} (League Phase)"
            elif league == "NATIONS":
                round_num = ci + 1
                r_name = f"Matchday {round_num}"
            else:
                mw_num = max(1, (base_played + 1) + offset)
                r_name = f"Matchweek {mw_num}"

            for j in c:
                raw_evs[j]["matchweek"] = r_name
                raw_evs[j]["date_span"] = d_span

        # Always keep the last 5 completed matchweeks + active/upcoming matchweeks
        start_ci = max(0, first_active_idx - 5)
        end_ci = min(len(clusters), first_active_idx + 2)
        kept_indices = [j for c in clusters[start_ci:end_ci] for j in c]
        kept_evs = [raw_evs[j] for j in kept_indices]

        # Pre-match team names and recover ESPN closing odds ONLY for finished games not already in football-data/track
        need_espn_odds = []
        matched_pairs = []
        for ev in kept_evs:
            home = match_team(ev["home"], cands)
            away = match_team(ev["away"], cands)
            if league in ("UCL", "UEL", "NATIONS"):
                home = home or ev["home"]
                away = away or ev["away"]
            matched_pairs.append((ev, home, away))
            if ev.get("state") in ("in", "post") and not ev.get("espn_odds"):
                r_fd = fd.get((league, home, away)) if (home and away) else None
                has_fd_odds = r_fd is not None and not pd.isna(r_fd.oh_avg)
                has_tr_odds = ev["espn_id"] in track and bool(track[ev["espn_id"]].get("best_odds"))
                if not has_fd_odds and not has_tr_odds:
                    need_espn_odds.append(ev)

        if need_espn_odds:
            espn.soccer_recover_odds(lg["espn"], need_espn_odds)

        # Pre-compute recent completed match IDs per team for pre-match lineup rotation detection
        now_utc = pd.Timestamp.now(tz="UTC")
        recent_post_by_team: dict[str, list[tuple[str, bool]]] = {}
        for ev, h_m, a_m in matched_pairs:
            if ev.get("state") == "post" and h_m and a_m:
                recent_post_by_team.setdefault(h_m, []).append((ev["espn_id"], True))
                recent_post_by_team.setdefault(a_m, []).append((ev["espn_id"], False))
        usual_xi_cache: dict[str, set[str]] = {}

        def _get_usual_starters(t_key: str) -> set[str]:
            if t_key in usual_xi_cache:
                return usual_xi_cache[t_key]
            recent_games = recent_post_by_team.get(t_key, [])[-4:]
            counts: dict[str, int] = {}
            for eid, is_home_side in recent_games:
                try:
                    s_sum = espn.soccer_summary(lg["espn"], eid)
                    side_key = "home" if is_home_side else "away"
                    for pl in ((s_sum.get("lineups") or {}).get(side_key) or {}).get("starters") or []:
                        if pl:
                            counts[pl] = counts.get(pl, 0) + 1
                except Exception:
                    pass
            min_starts = 2 if len(recent_games) >= 3 else 1
            usual = {pl for pl, c in counts.items() if c >= min_starts}
            usual_xi_cache[t_key] = usual
            return usual

        std_rows = espn.standings(lg["espn"]) or []
        std_by_team: dict[str, dict] = {}
        std_names: list[str] = []
        for srow in std_rows:
            t_disp = srow.get("team") or ""
            if t_disp:
                std_by_team[t_disp] = srow
                std_by_team[t_disp.lower()] = srow
                std_names.append(t_disp)

        def _match_std_team(tname: str | None) -> str | None:
            if not tname:
                return None
            if tname in std_by_team:
                return std_by_team[tname]["team"]
            if tname.lower() in std_by_team:
                return std_by_team[tname.lower()]["team"]
            m_name = match_team(tname, std_names)
            return std_by_team[m_name]["team"] if m_name and m_name in std_by_team else None

        # Reconstruct point-in-time league standings entering each Matchweek by unwinding completed matches
        has_multi_groups = sum(1 for r in std_rows if r.get("rank") == 1) > 1
        mw_standings: dict[str, dict[str, dict]] = {}
        if std_rows and not has_multi_groups:
            mw_min_kick: dict[str, str] = {}
            for ev_item in kept_evs:
                mw_lbl = ev_item.get("matchweek") or ""
                k_iso = ev_item.get("kickoff") or ""
                if mw_lbl and (mw_lbl not in mw_min_kick or k_iso < mw_min_kick[mw_lbl]):
                    mw_min_kick[mw_lbl] = k_iso

            for mw_lbl, cutoff_iso in mw_min_kick.items():
                snap = {
                    r["team"]: {
                        "team": r["team"],
                        "points": int(r.get("points") or 0),
                        "gd": int(r.get("gd") or 0),
                        "played": int(r.get("played") or 0),
                        "orig_rank": int(r.get("rank") or 99),
                    }
                    for r in std_rows if r.get("team")
                }
                for ev_other in raw_evs:
                    if ev_other.get("state") != "post" or (ev_other.get("kickoff") or "") < cutoff_iso:
                        continue
                    try:
                        hs = int(ev_other["home_score"])
                        as_ = int(ev_other["away_score"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    ht = _match_std_team(ev_other.get("home"))
                    at = _match_std_team(ev_other.get("away"))
                    h_pts = 3 if hs > as_ else (1 if hs == as_ else 0)
                    a_pts = 3 if as_ > hs else (1 if hs == as_ else 0)
                    if ht and ht in snap:
                        snap[ht]["points"] = max(0, snap[ht]["points"] - h_pts)
                        snap[ht]["gd"] -= (hs - as_)
                        snap[ht]["played"] = max(0, snap[ht]["played"] - 1)
                    if at and at in snap:
                        snap[at]["points"] = max(0, snap[at]["points"] - a_pts)
                        snap[at]["gd"] -= (as_ - hs)
                        snap[at]["played"] = max(0, snap[at]["played"] - 1)

                ranked_snap = sorted(
                    snap.values(),
                    key=lambda x: (-x["points"], -x["gd"], x["orig_rank"]),
                )
                mw_map: dict[str, dict] = {}
                for r_idx, r_obj in enumerate(ranked_snap, 1):
                    entry = {**r_obj, "rank": r_idx}
                    mw_map[r_obj["team"]] = entry
                    mw_map[r_obj["team"].lower()] = entry
                mw_standings[mw_lbl] = mw_map

        def _find_fb_standing(tname: str | None, mw_lbl: str | None = None) -> dict | None:
            canon_t = _match_std_team(tname)
            if not canon_t:
                return None
            if mw_lbl and mw_lbl in mw_standings:
                return mw_standings[mw_lbl].get(canon_t) or std_by_team.get(canon_t)
            return std_by_team.get(canon_t)

        out = []
        for ev, home, away in matched_pairs:
            mw_lbl = ev.get("matchweek")
            h_std = _find_fb_standing(ev.get("home"), mw_lbl) or _find_fb_standing(home, mw_lbl)
            a_std = _find_fb_standing(ev.get("away"), mw_lbl) or _find_fb_standing(away, mw_lbl)
            mw_suffix = f" entering {mw_lbl}" if (ev.get("state") == "post" and mw_lbl) else ""
            item = {
                **ev,
                "league": league,
                "league_name": lg["name"],
                "home_fd": home,
                "away_fd": away,
                "home_rank": h_std["rank"] if h_std and h_std.get("rank") else None,
                "away_rank": a_std["rank"] if a_std and a_std.get("rank") else None,
                "home_standing_info": (
                    f"#{h_std['rank']} in {lg['name']}{mw_suffix} ({h_std['points']} pts, {h_std['gd']:+d} Goal Dif)"
                    if h_std and h_std.get("rank") else None
                ),
                "away_standing_info": (
                    f"#{a_std['rank']} in {lg['name']}{mw_suffix} ({a_std['points']} pts, {a_std['gd']:+d} Goal Dif)"
                    if a_std and a_std.get("rank") else None
                ),
            }
            if not home or not away:
                item["error"] = "team not matched to statistics database"
                out.append(item)
                continue
            # ---- odds sources (French Winamax.fr prioritized first for live/upcoming, then historical/closing fallback)
            books = {}
            if ev.get("state") != "post":
                for o in oapi:
                    if (match_team(o["home"], [home, ev["home"]]) or match_team(o.get("home_fr", ""), [home, ev["home"]])) and (
                        match_team(o["away"], [away, ev["away"]]) or match_team(o.get("away_fr", ""), [away, ev["away"]])
                    ):
                        prov = o.get("provider", "Winamax.fr")
                        books[prov] = {"avg": o["1x2"]["avg"], "max": o["1x2"]["max"], "books": o["1x2"]["books"],
                                       "ou25_avg": o["ou25"]["avg"] if o.get("ou25") else None}
            r = fd.get((league, home, away))
            if r is not None and not pd.isna(r.oh_avg):
                books["football-data (avg of bookmakers)"] = {"avg": [r.oh_avg, r.od_avg, r.oa_avg],
                                                              "max": [r.oh_max, r.od_max, r.oa_max],
                                                              "b365": [r.oh_b365, r.od_b365, r.oa_b365],
                                                              "ou25_avg": [r.oo25_avg, r.ou25_avg]}
            if ev.get("espn_odds"):
                books[ev["espn_odds"]["provider"]] = {"avg": ev["espn_odds"]["1x2"], "max": ev["espn_odds"]["1x2"]}
            elif ev["espn_id"] in track and track[ev["espn_id"]].get("best_odds"):
                tr = track[ev["espn_id"]]
                books["Tracked Closing Odds"] = {
                    "avg": tr.get("market_odds") or tr.get("best_odds"),
                    "max": tr.get("best_odds"),
                }
            if "Stake.com" in books:
                market_odds = books["Stake.com"]["avg"]
                best = books["Stake.com"]["max"]
            elif "Winamax.fr" in books:
                market_odds = books["Winamax.fr"]["avg"]
                best = books["Winamax.fr"]["max"]
            else:
                market_odds = next((b["avg"] for b in books.values() if b.get("avg") and all(x and x == x for x in b["avg"])), None)
                best = None
                for b in books.values():
                    m = b.get("max")
                    if m and all(x and x == x for x in m):
                        best = m if best is None else [max(x, y) for x, y in zip(best, m)]
            ou_odds = next((b["ou25_avg"] for b in books.values() if b.get("ou25_avg") and all(x and x == x for x in b["ou25_avg"])), None)
            # ---- absences (manual + live EPL/SportsGambler injuries for upcoming/live fixtures)
            man = settings["manual_absences"].get(ev["espn_id"], {})
            live_adj = {"home_out": float(man.get("home", 0)), "away_out": float(man.get("away", 0))}
            inj_list = []
            h_inj_players = []
            a_inj_players = []
            h_rot_players = []
            a_rot_players = []
            if ev.get("state") != "post" and fb_inj_teams:
                hk = match_team(ev.get("home") or "", fb_inj_teams) or match_team(home, fb_inj_teams)
                ak = match_team(ev.get("away") or "", fb_inj_teams) or match_team(away, fb_inj_teams)
                if hk:
                    h_inj_players = fb_inj_map.get(hk, [])
                    live_adj["home_out"] += round(sum(float(p.get("weight", 0.0)) for p in h_inj_players), 2)
                if ak:
                    a_inj_players = fb_inj_map.get(ak, [])
                    live_adj["away_out"] += round(sum(float(p.get("weight", 0.0)) for p in a_inj_players), 2)

            # Auto-score confirmed starting XI rotation when lineups are posted (~2-3h before kickoff or live)
            if ev.get("state") != "post":
                try:
                    hrs_to_kick = (pd.Timestamp(ev["kickoff"]) - now_utc).total_seconds() / 3600.0
                except Exception:
                    hrs_to_kick = 99.0
                if ev.get("state") == "in" or (-3.0 <= hrs_to_kick <= 3.0):
                    try:
                        l_sum = espn.soccer_summary(lg["espn"], ev["espn_id"])
                        lups = l_sum.get("lineups") or {}
                        for side_k, t_key, inj_pl, out_k, rot_list in (
                            ("home", home, h_inj_players, "home_out", h_rot_players),
                            ("away", away, a_inj_players, "away_out", a_rot_players),
                        ):
                            xi = set((lups.get(side_k) or {}).get("starters") or [])
                            if len(xi) >= 11:
                                usual = _get_usual_starters(t_key)
                                inj_names = {p["player"].lower().split()[-1] for p in inj_pl if p.get("player")}
                                for u_pl in sorted(usual):
                                    u_last = u_pl.lower().split()[-1]
                                    if u_pl not in xi and u_last not in inj_names:
                                        rot_list.append(u_pl)
                                if len(rot_list) >= 2:
                                    live_adj[out_k] = round(live_adj[out_k] + 0.35 * (len(rot_list) - 1), 2)
                    except Exception:
                        pass
            ts_kick = pd.Timestamp(ev["kickoff"])
            kick = ts_kick.tz_convert(None) if ts_kick.tzinfo is not None else ts_kick
            cached_pre = self.prematch_preds.get((league, home, away))
            if cached_pre is not None and ev.get("state") == "post" and not any(live_adj.values()):
                pred = {
                    "components": dict(cached_pre["components"]),
                    "features": cached_pre["features"],
                }
                if market_odds is not None:
                    fair = devig(market_odds)
                    if fair:
                        pred["components"]["market"] = {"p": fair, "odds": [float(o) for o in market_odds]}
            else:
                pred = eng.predict(league, C.CURRENT_SEASON, kick.normalize(), home, away, market_odds, live_adj)
            comps = pred["components"]
            p_final, used = fb_blend(comps, W)
            sw = {k: v for k, v in W.items() if k != "market"}
            p_stats, _ = fb_blend({k: v for k, v in comps.items() if k != "market"}, sw)
            p_market = comps.get("market", {}).get("p")
            # O/U 2.5 and BTTS: Poisson (weighted xg/goals) blended with market O/U
            wx, wg = sw.get("xg_poisson", .5), sw.get("goals_poisson", .5)
            tot = (wx + wg) or 1
            over = (wx * comps["xg_poisson"]["over25"] + wg * comps["goals_poisson"]["over25"]) / tot
            btts = (wx * comps["xg_poisson"]["btts"] + wg * comps["goals_poisson"]["btts"]) / tot
            over_model = over
            if ou_odds:
                f = devig(ou_odds)
                if f:
                    ms = float(settings["football_market_share"])
                    over = ms * f[0] + (1 - ms) * over
            item.update({
                "odds_books": {k: {kk: ([None if (x != x) else round(float(x), 2) for x in vv] if isinstance(vv, list) else vv)
                                   for kk, vv in v.items()} for k, v in books.items()},
                "market_odds": market_odds and [round(float(x), 2) for x in market_odds],
                "best_odds": best and [round(float(x), 2) for x in best],
                "components": {k: {**{kk: vv for kk, vv in v.items() if kk != "p"},
                                   "p": [round(x, 4) for x in v["p"]], "weight": round(used.get(k, 0), 4),
                                   "info": COMPONENT_INFO["football"][k]} for k, v in comps.items()},
                "features": pred["features"],
                "absences": {
                    "manual": man,
                    "api": inj_list,
                    "home_players": h_inj_players,
                    "away_players": a_inj_players,
                    "home_rotated": h_rot_players,
                    "away_rotated": a_rot_players,
                    "applied": live_adj,
                },
                "probs": [round(x, 4) for x in p_final],
                "probs_stats": [round(x, 4) for x in p_stats],
                "probs_market": p_market and [round(x, 4) for x in p_market],
                "over25": round(over, 4), "over25_model": round(over_model, 4), "btts": round(btts, 4),
                "top_score": comps["xg_poisson"]["top_score"],
                "expected_goals": comps["xg_poisson"]["lambda"],
                "recommendation": recommend(p_final, LABELS_1X2, best, market_odds, p_stats, p_market),
            })
            # ---- track record: freeze the latest pre-match prediction
            if ev["state"] == "pre":
                track[ev["espn_id"]] = {"sport": "football", "league": league, "kickoff": ev["kickoff"],
                                        "match": f"{ev['home']} - {ev['away']}", "probs": item["probs"],
                                        "pick": item["recommendation"]["pick_index"], "market_odds": item["market_odds"],
                                        "best_odds": item["best_odds"], "value": item["recommendation"]["value_bets"],
                                        "saved_at": datetime.now(timezone.utc).isoformat()}
            elif ev["state"] == "post" and ev["espn_id"] in track and "result" not in track[ev["espn_id"]]:
                hs, as_ = int(ev["home_score"] or 0), int(ev["away_score"] or 0)
                track[ev["espn_id"]]["result"] = 0 if hs > as_ else (1 if hs == as_ else 2)
                track[ev["espn_id"]]["score"] = f"{hs}-{as_}"
            if ev["espn_id"] in track:
                item["tracked"] = track[ev["espn_id"]]
            out.append(item)
        with _lock:
            _track_save(track)
            self._fixtures_cache[league] = (time.time(), out)

        if not with_finished:
            return [x for x in out if x.get("state") != "post"]
        return out

    def team_table(self, league: str) -> list[dict]:
        eng = self.ensure()
        rows = []
        for t in eng.teams_in(league, C.CURRENT_SEASON):
            ts = eng.teams[(league, t)]
            rows.append({"team": t, "elo": round(ts.elo), "xg_for": round(ts.att_xg, 2), "xg_against": round(ts.def_xg, 2),
                         "goals_for": round(ts.att_g, 2), "goals_against": round(ts.def_g, 2), "played": ts.s_games,
                         "points": ts.s_pts, "gd": ts.s_gd, "promoted": ts.promoted,
                         "form": "".join(r[3] for r in list(ts.recent)[-5:])})
        return sorted(rows, key=lambda r: -r["elo"])


# ================================================================== TENNIS
class TennisService:
    def __init__(self):
        self._lock = threading.RLock()
        self.engine: TennisEngine | None = None
        self.df: pd.DataFrame | None = None
        self.built_at = 0.0
        self._surface_cache: dict[str, str] = {}
        self._closing_odds_cache: dict[tuple[str, str], dict] = {}
        self._matches_cache: list[dict] = []
        self._matches_cache_time = 0.0
        self.recent_hist_preds: list[dict] = []

    def ensure(self, max_age: int = 6 * 3600):
        with self._lock:
            if self.engine is None or time.time() - self.built_at > max_age:
                df = tennisdata.load_all()
                eng = TennisEngine()
                hist_preds = []
                if not df.empty:
                    def _tn_cb(r, pred, a_is_w):
                        ws = int(r.wsets) if not pd.isna(r.wsets) else (2 if a_is_w else 0)
                        ls = int(r.lsets) if not pd.isna(r.lsets) else (0 if a_is_w else 2)
                        score_str = f"{ws}-{ls}" if a_is_w else f"{ls}-{ws}"
                        hist_preds.append({
                            "date": str(r.date)[:10],
                            "tour": r.tour,
                            "tournament": str(r.tournament),
                            "surface": str(r.surface),
                            "a": pred["a"],
                            "b": pred["b"],
                            "a_is_w": a_is_w,
                            "score": score_str,
                            "components": pred["components"],
                            "odds_avg": pred.get("odds_avg"),
                            "odds_max": pred.get("odds_max"),
                        })

                    eng.run(df, callback=_tn_cb, callback_min_date="2026-08-01")
                    # Pre-index closing odds for recent matches only to keep memory footprint tiny
                    c_odds = {}
                    recent_df = df[df["year"] >= 2026] if "year" in df.columns else df
                    for r in recent_df.itertuples():
                        if not pd.isna(r.avgw) and not pd.isna(r.avgl):
                            c_odds[(r.winner, r.loser)] = {
                                "avg": [round(float(r.avgw), 2), round(float(r.avgl), 2)],
                                "max": [round(float(getattr(r, "maxw", r.avgw) or r.avgw), 2), round(float(getattr(r, "maxl", r.avgl) or r.avgl), 2)],
                            }
                    self._closing_odds_cache = c_odds
                    slim_df = df.drop_duplicates("tournament", keep="last")[["tournament", "location", "surface"]].copy()
                else:
                    slim_df = df
                del df
                gc.collect()
                self.engine, self.df, self.recent_hist_preds, self.built_at = eng, slim_df, hist_preds, time.time()
        return self.engine

    def _surface(self, tournament: str) -> str:
        if not tournament:
            return "Hard"
        if tournament in self._surface_cache:
            return self._surface_cache[tournament]
        t_low = tournament.lower()

        # 1. Explicit authoritative rules for tournaments sharing cities (e.g. Paris Masters vs Roland Garros)
        HARD_KEYWORDS = (
            "paris masters", "bnp paribas masters", "rolex paris", "australian open", "us open",
            "indian wells", "miami", "shanghai", "cincinnati", "canadian", "montreal", "toronto",
            "china open", "beijing", "japan open", "tokyo", "pan pacific", "erste bank", "vienna",
            "swiss indoors", "basel", "european open", "antwerp", "brussels", "almaty", "stockholm",
            "moselle", "metz", "atp finals", "wta finals", "next gen", "rotterdam", "marseille",
            "montpellier", "auvergne", "doha", "dubai", "acapulco", "dallas", "delray", "washington",
            "winston-salem", "chengdu", "hangzhou", "wuhan", "ningbo", "guangzhou", "hong kong",
            "korea open", "seoul", "chennai", "linz", "guadalajara", "monterrey", "merida",
            "rovereto", "valle d'aosta",
        )
        GRASS_KEYWORDS = (
            "wimbledon", "halle", "queen", "eastbourne", "hertogenbosch", "libema",
            "mallorca", "bad homburg", "birmingham", "nottingham", "newport", "boss open",
        )
        CLAY_KEYWORDS = (
            "roland garros", "french open", "monte-carlo", "monte carlo", "madrid",
            "internazionali", "rome", "roma", "barcelona", "hamburg", "bastad", "gstaad",
            "umag", "kitzbuhel", "estoril", "munich", "geneva", "bucharest", "marrakech",
            "rio open", "buenos aires", "santiago", "charleston", "strasbourg", "rabat",
            "palermo", "iasi", "budapest", "prague", "lausanne", "bogota", "cali",
            "curitiba", "tolentino", "lisboa", "belem",
        )
        if any(k in t_low for k in HARD_KEYWORDS):
            self._surface_cache[tournament] = "Hard"
            return "Hard"
        if any(k in t_low for k in GRASS_KEYWORDS):
            self._surface_cache[tournament] = "Grass"
            return "Grass"
        if any(k in t_low for k in CLAY_KEYWORDS):
            self._surface_cache[tournament] = "Clay"
            return "Clay"

        if self.df is None or self.df.empty:
            return "Hard"

        # 2. Match against historical tournament names first (before location fallback)
        t = self.df.drop_duplicates("tournament", keep="last")
        t_names = {str(r.tournament): r.surface for r in t.itertuples()}
        best = difflib.get_close_matches(t_low, [n.lower() for n in t_names], n=1, cutoff=0.6)
        res = "Hard"
        if best:
            for n, s in t_names.items():
                if n.lower() == best[0]:
                    res = s
                    break
        else:
            # 3. Only use location fallback for cities that do not host conflicting surfaces (exclude Paris/London/Stuttgart)
            loc_names = {
                str(r.location): r.surface
                for r in t.itertuples()
                if str(r.location).lower() not in ("paris", "london", "stuttgart")
            }
            clean = t_low.replace("rolex", "").replace("masters", "").replace("open", "").strip()
            best_loc = difflib.get_close_matches(clean, [n.lower() for n in loc_names], n=1, cutoff=0.6)
            if best_loc:
                for n, s in loc_names.items():
                    if n.lower() == best_loc[0]:
                        res = s
                        break
        self._surface_cache[tournament] = res
        return res

    def _tier(self, tour: str, tournament: str) -> str:
        if not tournament:
            return f"{tour.upper()} 250"
        t_low = tournament.lower()
        t_up = tour.upper()

        if any(k in t_low for k in ("australian open", "roland garros", "french open", "wimbledon", "us open")):
            return "Grand Slam"
        if any(k in t_low for k in ("atp finals", "wta finals", "next gen")):
            return "Tour Finals"

        if t_up == "ATP":
            if any(k in t_low for k in (
                "indian wells", "bnp paribas open", "miami open", "monte-carlo", "monte carlo",
                "madrid", "internazionali", "rome", "canadian", "omnium banque", "national bank",
                "cincinnati", "western & southern", "shanghai", "paris masters", "rolex paris", "bnp paribas masters",
            )):
                return "Masters 1000"
            if any(k in t_low for k in (
                "rotterdam", "abn amro", "rio open", "dubai", "acapulco", "mexican open", "barcelona",
                "munich", "dallas", "doha", "qatar", "halle", "terra wortmann", "queen", "cinch",
                "hamburg", "washington", "mubadala citi", "china open", "beijing", "japan open",
                "erste bank", "vienna", "swiss indoors", "basel",
            )):
                return "ATP 500"
            return "ATP 250"

        # WTA tiers
        if any(k in t_low for k in (
            "doha", "qatar", "dubai", "indian wells", "bnp paribas open", "miami", "madrid",
            "internazionali", "rome", "canadian", "omnium banque", "national bank", "cincinnati",
            "western & southern", "china open", "beijing", "wuhan",
        )):
            return "WTA 1000"
        if any(k in t_low for k in (
            "brisbane", "adelaide", "united cup", "abu dhabi", "linz", "charleston", "stuttgart",
            "porsche", "strasbourg", "berlin", "bad homburg", "eastbourne", "washington",
            "mubadala", "san diego", "monterrey", "guadalajara", "korea open", "seoul",
            "ningbo", "pan pacific", "tokyo", "zhengzhou", "merida",
        )):
            return "WTA 500"
        if any(k in t_low for k in (
            "rovereto", "valle d'aosta", "curitiba", "queretaro", "cali", "angola", "tolentino",
            "porto", "ankara", "jingshan", "adana", "suzhou", "samsun", "mallorca women",
            "lisboa", "belem", "tampico", "midland", "colina", "florianopolis", "limoges", "angers",
        )):
            return "WTA 125"
        return "WTA 250"

    def matches(self, force_refresh: bool = False) -> list[dict]:
        with _lock:
            if not force_refresh and self._matches_cache and (time.time() - self._matches_cache_time < 600):
                return self._matches_cache

        eng = self.ensure()
        W = weights("tennis")
        oapi = optional_apis.live_tennis_odds()
        out = []
        for tour in ("atp", "wta"):
            known = eng.known_players(tour.upper())
            live_ranks = espn.tennis_rankings(tour)
            for m in espn.tennis_matches(tour):
                tier = self._tier(tour, m["tournament"])
                if "125" in tier or "250" in tier or "Challenger" in tier:
                    continue
                a = match_player(m["a"], known)
                b = match_player(m["b"], known)
                ra_live = live_ranks.get((m["a"] or "").lower())
                rb_live = live_ranks.get((m["b"] or "").lower())
                surface = self._surface(m["tournament"])
                item = {**m, "surface": surface, "tier": tier, "a_db": a, "b_db": b,
                        "a_rank": ra_live, "b_rank": rb_live}
                if not known:
                    item["error"] = "no tennis history loaded (add tennis-data files)"
                    out.append(item)
                    continue
                odds = None
                # 1. Match live/upcoming matches against live real-time bookmaker odds feed
                if m.get("state") != "post":
                    last_a = m["a"].split()[-1].lower()
                    last_b = m["b"].split()[-1].lower()
                    for o in oapi:
                        o_last_a = o["a"].split()[-1].lower()
                        o_last_b = o["b"].split()[-1].lower()
                        if {last_a, last_b} == {o_last_a, o_last_b}:
                            if last_a == o_last_a:
                                odds = o["avg"]
                                item["best_odds"] = o.get("max", o["avg"])
                            else:
                                odds = o["avg"][::-1]
                                item["best_odds"] = o.get("max", o["avg"])[::-1]
                            item["odds_books"] = {o.get("provider", "Bookmaker"): {"avg": odds, "max": item["best_odds"]}}
                            break

                # 2. Instant O(1) match completed/past matches against historical closing odds index
                if not odds and self._closing_odds_cache:
                    pa = a or m["a"]
                    pb = b or m["b"]
                    h = self._closing_odds_cache.get((pa, pb))
                    if h:
                        odds = h["avg"]
                        item["best_odds"] = h["max"]
                        item["odds_books"] = {"tennis-data (closing consensus)": {"avg": odds, "max": item["best_odds"]}}
                    else:
                        h_rev = self._closing_odds_cache.get((pb, pa))
                        if h_rev:
                            odds = h_rev["avg"][::-1]
                            item["best_odds"] = h_rev["max"][::-1]
                            item["odds_books"] = {"tennis-data (closing consensus)": {"avg": odds, "max": item["best_odds"]}}

                pred = eng.predict(tour.upper(), a or m["a"], b or m["b"], surface, m["best_of"],
                                   pd.Timestamp(m["start"]).tz_convert(None) if m["start"] else pd.Timestamp.now(), odds,
                                   rank_a=ra_live, rank_b=rb_live)
                comps = pred["components"]
                pa, used = tn_blend(comps, W)
                sw = {k: v for k, v in W.items() if k != "market"}
                ps, _ = tn_blend({k: v for k, v in comps.items() if k != "market"}, sw)
                pm = comps.get("market", {}).get("p")
                feat_ranks = pred["features"].get("rank", [300, 300])
                hist_ra = feat_ranks[0] if feat_ranks[0] and feat_ranks[0] < 300 else None
                hist_rb = feat_ranks[1] if feat_ranks[1] and feat_ranks[1] < 300 else None
                if m.get("state") == "post":
                    a_rank_disp = hist_ra or ra_live
                    b_rank_disp = hist_rb or rb_live
                else:
                    a_rank_disp = ra_live or hist_ra
                    b_rank_disp = rb_live or hist_rb
                item.update({
                    "a_rank": a_rank_disp,
                    "b_rank": b_rank_disp,
                    "components": {k: {**{kk: vv for kk, vv in v.items() if kk != "p"}, "p": round(v["p"], 4),
                                       "weight": round(used.get(k, 0), 4), "info": COMPONENT_INFO["tennis"][k]}
                                   for k, v in comps.items()},
                    "features": pred["features"], "market_odds": odds,
                    "probs": [round(pa, 4), round(1 - pa, 4)], "probs_stats": [round(ps, 4), round(1 - ps, 4)],
                    "probs_market": pm and [round(pm, 4), round(1 - pm, 4)],
                    "unknown_players": [n for n, k in ((m["a"], a), (m["b"], b)) if not k],
                    "recommendation": recommend([pa, 1 - pa], [m["a"], m["b"]], item.get("best_odds"), odds,
                                                [ps, 1 - ps], pm and [pm, 1 - pm]),
                })
                out.append(item)
        with _lock:
            self._matches_cache = out
            self._matches_cache_time = time.time()
        return out


# ================================================================== NBA
class NBAService:
    def __init__(self):
        self._lock = threading.RLock()
        self.engine: NBAEngine | None = None
        self.built_at = 0.0
        self._all_games_cache: list[dict] = []
        self._all_games_cache_time = 0.0

    def ensure(self, max_age: int = 3600):
        with self._lock:
            if self.engine is None or time.time() - self.built_at > max_age:
                eng = NBAEngine()
                try:
                    res = espn.nba_season_results(date.fromisoformat(C.NBA_SEASON_START))
                    if res:
                        eng.update_with_results(res)
                    else:
                        std = espn.nba_standings()
                        if std:
                            eng.ingest_standings(std)
                except Exception:
                    std = espn.nba_standings()
                    if std:
                        eng.ingest_standings(std)
                self.engine, self.built_at = eng, time.time()
        return self.engine

    @staticmethod
    def _eval_team_absences(team_injuries: list[dict], team_leaders: list[str]) -> tuple[float, list[dict]]:
        """Convert ESPN injury/DNP list into weighted star-absence units and annotated player list."""
        star_map = getattr(C, "NBA_STAR_WEIGHTS", {})
        leader_set = {nm.lower() for nm in (team_leaders or [])}
        total_units = 0.0
        notable: list[dict] = []
        for inj in team_injuries or []:
            p_name = inj.get("player") or ""
            status = inj.get("status") or "Out"
            st_low = status.lower()
            detail = inj.get("detail") or ""
            detail_low = detail.lower()
            if "coach" in detail_low:
                continue
            if "out" in st_low or "suspension" in st_low or "doubtful" in st_low:
                sev = 1.0 if "doubtful" not in st_low else 0.75
            elif "day-to-day" in st_low or "questionable" in st_low:
                sev = 0.35
            else:
                sev = 0.5

            base_w = star_map.get(p_name, 0.0)
            if base_w == 0.0 and p_name.lower() in leader_set:
                base_w = 0.85
            if base_w == 0.0:
                # For unranked rotation players, only count confirmed Out/Doubtful injuries (not minor DTD)
                if sev < 0.75:
                    continue
                base_w = 0.20

            impact = round(base_w * sev, 2)
            if impact >= 0.15:
                total_units += impact
                notable.append({
                    "player": p_name,
                    "status": status,
                    "detail": detail,
                    "impact_pts": round(impact * C.NBA_PARAMS["injury_pts_per_star"], 1),
                    "is_star": base_w >= 0.50,
                })
        return round(min(total_units, 3.8), 2), notable

    def _engine_as_of(self, results: list[dict], as_of: str, cache: dict) -> tuple[NBAEngine, dict[str, str]]:
        """Engine rated only on games completed before `as_of` (no look-ahead)."""
        if as_of in cache:
            return cache[as_of]
        eng = NBAEngine()
        last_game = eng.update_with_results(results, as_of=as_of)
        cache[as_of] = (eng, last_game)
        return cache[as_of]

    def all_season_games(self, force_refresh: bool = False) -> list[dict]:
        with _lock:
            if not force_refresh and self._all_games_cache and (time.time() - self._all_games_cache_time < 600):
                return self._all_games_cache

        fallback_eng = self.ensure()
        W = weights("nba")
        settings = load_settings()
        oapi = optional_apis.live_nba_odds()
        live_inj = espn.nba_injuries()
        today_dt = datetime.now(timezone.utc).date()
        s_start = date.fromisoformat(C.NBA_SEASON_START)
        days_back = max(8, (today_dt - s_start).days)
        events = espn.nba_fixtures(days_back=days_back, days_ahead=7)
        track = _track_load()
        changed = False
        try:
            season_results = espn.nba_season_results(s_start)
            pace_by_id = {}
            for ev in events:
                ep = ev.get("espn_pace") or {}
                if ep:
                    pace_by_id[ev["espn_id"]] = round(sum(ep.values()) / len(ep), 1)
            for sr in season_results:
                if sr["espn_id"] in pace_by_id:
                    sr["pace"] = pace_by_id[sr["espn_id"]]
        except Exception:
            season_results = []
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        eng_cache: dict = {}

        # Compute current All-NBA overall league standing (1..30) + conference seed
        nba_std_rows = espn.nba_standings() or []
        team_conf_map: dict[str, str] = {}
        sorted_nba = sorted(
            nba_std_rows,
            key=lambda r: (
                -(r.get("wins", 0) / max(1, r.get("wins", 0) + r.get("losses", 0))),
                -r.get("wins", 0),
                -r.get("diff", 0.0),
            ),
        )
        nba_rank_by_team: dict[str, dict] = {}
        for idx, r in enumerate(sorted_nba, 1):
            conf_short = "East" if "East" in (r.get("conference") or "") else "West"
            team_conf_map[r["team"]] = conf_short
            entry = {
                "rank": idx,
                "conf_seed": r.get("seed"),
                "conf": conf_short,
                "record": f"{r.get('wins', 0)}-{r.get('losses', 0)}",
            }
            nba_rank_by_team[r["team"]] = entry
            nba_rank_by_team[r["team"].lower()] = entry

        pit_nba_ranks_cache: dict[str, dict[str, dict]] = {}

        def _nba_standings_at(eng_pit: NBAEngine, day_key: str) -> dict[str, dict]:
            if day_key in pit_nba_ranks_cache:
                return pit_nba_ranks_cache[day_key]
            all_t = list(eng_pit.teams.values())
            all_t.sort(
                key=lambda t: (
                    -(t.wins / max(1, t.wins + t.losses)),
                    -t.wins,
                    -(t.ppg_for - t.ppg_against),
                    -t.elo,
                )
            )
            conf_counts = {"East": 0, "West": 0}
            rmap: dict[str, dict] = {}
            for idx_t, t in enumerate(all_t, 1):
                c_sh = team_conf_map.get(t.name, "NBA")
                conf_counts[c_sh] = conf_counts.get(c_sh, 0) + 1
                ent = {
                    "rank": idx_t,
                    "conf_seed": conf_counts[c_sh],
                    "conf": c_sh,
                    "record": f"{t.wins}-{t.losses}",
                }
                rmap[t.name] = ent
                rmap[t.name.lower()] = ent
            pit_nba_ranks_cache[day_key] = rmap
            return rmap

        out = []

        for ev in events:
            if ev.get("season_type") not in (2, None):
                continue
            home = ev["home"]
            away = ev["away"]
            ts_kick = pd.Timestamp(ev["kickoff"])
            kick_day = (ts_kick.tz_convert(None) if ts_kick.tzinfo is not None else ts_kick).normalize()

            # Point-in-time ratings: finished/live games use only results before tip-off
            if season_results:
                as_of = min(ev["kickoff"], now_iso)
                eng, last_game = self._engine_as_of(season_results, as_of, eng_cache)
            else:
                eng, last_game = fallback_eng, {}

            def _rest(team: str) -> int | None:
                if team not in last_game:
                    return None
                lg = pd.Timestamp(last_game[team]).tz_convert(None).normalize()
                d = int((kick_day - lg).days)
                return d if d > 0 else None

            rh, ra = _rest(home), _rest(away)

            if ev.get("state") == "post" and season_results:
                pit_map = _nba_standings_at(eng, str(kick_day)[:10])
                h_nba_std = pit_map.get(home) or pit_map.get((home or "").lower()) or nba_rank_by_team.get(home)
                a_nba_std = pit_map.get(away) or pit_map.get((away or "").lower()) or nba_rank_by_team.get(away)
                pit_tag = f" on {str(kick_day)[:10]}"
            else:
                h_nba_std = nba_rank_by_team.get(home) or nba_rank_by_team.get((home or "").lower())
                a_nba_std = nba_rank_by_team.get(away) or nba_rank_by_team.get((away or "").lower())
                pit_tag = ""
            item = {
                **ev,
                "home_rank": h_nba_std["rank"] if h_nba_std else None,
                "away_rank": a_nba_std["rank"] if a_nba_std else None,
                "home_standing_info": (
                    f"#{h_nba_std['rank']} NBA{pit_tag} (#{h_nba_std['conf_seed']} {h_nba_std['conf']} · {h_nba_std['record']})"
                    if h_nba_std else None
                ),
                "away_standing_info": (
                    f"#{a_nba_std['rank']} NBA{pit_tag} (#{a_nba_std['conf_seed']} {a_nba_std['conf']} · {a_nba_std['record']})"
                    if a_nba_std else None
                ),
            }
            books = {}
            ou_line = None
            spread_line = None

            # 1. Live French Winamax.fr & Bovada NBA odds (only for live/upcoming games)
            if ev.get("state") != "post":
                h_nick = home.split()[-1].lower()
                a_nick = away.split()[-1].lower()
                for o in oapi:
                    oh_nick = o["home"].split()[-1].lower()
                    oa_nick = o["away"].split()[-1].lower()
                    if {oh_nick, oa_nick} == {h_nick, a_nick}:
                        prov = o.get("provider", "Winamax.fr")
                        if prov not in books:
                            vals = o["avg"] if oh_nick == h_nick else o["avg"][::-1]
                            books[prov] = {"avg": vals, "max": vals}
                        if ou_line is None and o.get("ou_line"):
                            ou_line = o["ou_line"]
                        if spread_line is None and o.get("spread") is not None:
                            spread_line = o["spread"]

            # 2. ESPN DraftKings odds (and closing pickcenter odds for finished games)
            if ev.get("espn_odds"):
                eo = ev["espn_odds"]
                books[eo.get("provider", "DraftKings")] = {"avg": eo["ml"], "max": eo["ml"]}
                if ou_line is None:
                    ou_line = eo.get("ou_line")
                if spread_line is None:
                    spread_line = eo.get("spread")

            # 3. Persisted closing odds for completed games
            if not books and f"nba_{ev['espn_id']}" in track:
                tr = track[f"nba_{ev['espn_id']}"]
                if tr.get("best_odds"):
                    books["Tracked Closing Odds"] = {
                        "avg": tr.get("market_odds") or tr.get("best_odds"),
                        "max": tr.get("best_odds"),
                    }
                    ou_line = tr.get("ou_line")
                    spread_line = tr.get("spread_line")

            if "Stake.com" in books:
                market_odds = books["Stake.com"]["avg"]
                best_odds = books["Stake.com"]["max"]
            elif "Winamax.fr" in books:
                market_odds = books["Winamax.fr"]["avg"]
                best_odds = books["Winamax.fr"]["max"]
            else:
                market_odds = next((b["avg"] for b in books.values() if b.get("avg") and all(x and x == x for x in b["avg"])), None)
                best_odds = None
                for b in books.values():
                    m = b.get("max")
                    if m and all(x and x == x for x in m):
                        best_odds = m if best_odds is None else [max(x, y) for x, y in zip(best_odds, m)]

            # Automatic star/starter injury & DNP detection (plus optional manual override)
            ev_abs = ev.get("espn_absences") or {}
            h_inj_list = ev_abs.get(home) if home in ev_abs else live_inj.get(home, [])
            a_inj_list = ev_abs.get(away) if away in ev_abs else live_inj.get(away, [])
            auto_h_out, h_notable = self._eval_team_absences(h_inj_list, ev.get("home_leaders") or [])
            auto_a_out, a_notable = self._eval_team_absences(a_inj_list, ev.get("away_leaders") or [])
            man = settings["manual_absences"].get(ev["espn_id"], {})
            live_adj = {
                "home_out": round(auto_h_out + float(man.get("home", 0)), 2),
                "away_out": round(auto_a_out + float(man.get("away", 0)), 2),
            }

            pred = eng.predict(
                home=home,
                away=away,
                market_odds=market_odds,
                ou_line=ou_line,
                spread_line=spread_line,
                live_adj=live_adj,
                rest_days=(rh, ra),
            )
            comps = pred["components"]
            sw = {k: v for k, v in W.items() if k != "market"}
            ps_home, _ = nba_blend({k: v for k, v in comps.items() if k != "market"}, sw)
            pm_home = comps.get("market", {}).get("p")

            # Divergence rule: if the pure stats model favors one side due to injuries/form
            # but market odds contradict it with heavy opposing bias and severe net injury differential (>= 2.5 pts):
            # trust the statistical model so casual public market bias doesn't force a pick against heavy injuries
            if pm_home is not None:
                net_inj = (live_adj.get("away_out", 0.0) - live_adj.get("home_out", 0.0)) * C.NBA_PARAMS["injury_pts_per_star"]
                if (ps_home < 0.50 and pm_home > 0.50 and net_inj <= -2.5) or (ps_home > 0.50 and pm_home < 0.50 and net_inj >= 2.5):
                    p_home = ps_home
                    used = {k: sw.get(k, 0.0) for k in comps if k != "market"}
                else:
                    p_home, used = nba_blend(comps, W)
            else:
                p_home, used = nba_blend(comps, W)

            probs = [round(p_home, 4), round(1.0 - p_home, 4)]
            probs_stats = [round(ps_home, 4), round(1.0 - ps_home, 4)]
            probs_market = [round(pm_home, 4), round(1.0 - pm_home, 4)] if pm_home is not None else None
            reco = recommend(probs, [home, away], best_odds, market_odds, probs_stats, probs_market)

            # Group by NBA Gameday (US Eastern / local gameday)
            gd_dt = ts_kick - pd.Timedelta(hours=5)
            gameday_label = gd_dt.strftime("%a, %b %d")

            item.update({
                "gameday": f"NBA Gameday — {gameday_label}",
                "components": {
                    k: {
                        **{kk: vv for kk, vv in v.items() if kk != "p"},
                        "p": round(v["p"], 4),
                        "weight": round(used.get(k, 0.0), 4),
                        "info": COMPONENT_INFO["nba"].get(k, ""),
                    }
                    for k, v in comps.items()
                },
                "features": pred["features"],
                "proj_home": pred["proj_home"],
                "proj_away": pred["proj_away"],
                "proj_total": pred["proj_total"],
                "proj_margin": pred["proj_margin"],
                "ou_line": pred["ou_line"],
                "over_prob": pred["over_prob"],
                "spread_line": pred["spread_line"],
                "odds_books": books,
                "market_odds": market_odds,
                "best_odds": best_odds,
                "probs": probs,
                "probs_stats": probs_stats,
                "probs_market": probs_market,
                "absences": {
                    "applied": live_adj,
                    "home_players": h_notable,
                    "away_players": a_notable,
                },
                "recommendation": reco,
            })

            # Persist closing odds & predictions in track record
            tkey = f"nba_{ev['espn_id']}"
            if ev["state"] == "pre" and tkey not in track:
                track[tkey] = {
                    "sport": "nba",
                    "kickoff": ev["kickoff"],
                    "home": home,
                    "away": away,
                    "probs": probs,
                    "pick": reco["pick_index"],
                    "best_odds": best_odds,
                    "market_odds": market_odds,
                    "ou_line": pred["ou_line"],
                    "spread_line": pred["spread_line"],
                }
                changed = True
            elif ev["state"] == "pre" and tkey in track and best_odds and not track[tkey].get("best_odds"):
                track[tkey]["best_odds"] = best_odds
                track[tkey]["market_odds"] = market_odds
                changed = True

            out.append(item)

        if changed:
            _track_save(track)

        with _lock:
            self._all_games_cache = out
            self._all_games_cache_time = time.time()
        return out

    def games(self, with_finished: bool = True, force_refresh: bool = False) -> list[dict]:
        all_g = self.all_season_games(force_refresh=force_refresh)
        if not with_finished:
            return [x for x in all_g if x.get("state") != "post"]
        # Keep the last 5 completed gamedays + all live/upcoming gamedays
        gds: dict[str, list[dict]] = {}
        for x in all_g:
            gds.setdefault(x.get("gameday", ""), []).append(x)
        completed_gds = [k for k, grp in gds.items() if all(g.get("state") == "post" for g in grp)]
        drop_gds = set(completed_gds[:-5]) if len(completed_gds) > 5 else set()
        if drop_gds:
            return [x for x in all_g if x.get("gameday", "") not in drop_gds]
        return list(all_g)


football = FootballService()
tennis = TennisService()
nba = NBAService()

DAILY_PICKS_FILE = C.BASE_DIR / "data" / "daily_picks_cache.json"
_daily_picks_cache: dict = {}
_daily_picks_cache_time: float = 0.0
_warmup_running: bool = False


def invalidate_all_caches() -> None:
    global _daily_picks_cache, _daily_picks_cache_time
    with _lock:
        football._fixtures_cache.clear()
        tennis._matches_cache = []
        tennis._matches_cache_time = 0.0
        nba._all_games_cache = []
        nba._all_games_cache_time = 0.0
        _daily_picks_cache = {}
        _daily_picks_cache_time = 0.0
        try:
            if DAILY_PICKS_FILE.exists():
                DAILY_PICKS_FILE.unlink()
        except Exception:
            pass


def _compute_summary(days_dict: dict[str, dict], sport_key: str | None = None) -> dict:
    total_days = 0
    settled_picks = 0
    hits = 0
    lock1_settled = 0
    lock1_hits = 0
    sweep_days = 0
    two_plus_days = 0
    full_settled_days = 0
    profit_u = 0.0

    for d_str, d_obj in days_dict.items():
        picks = d_obj["top3"] if sport_key is None else d_obj.get("by_sport", {}).get(sport_key, [])
        if not picks:
            continue
        total_days += 1
        d_settled = 0
        d_hits = 0
        for idx, p in enumerate(picks):
            if p.get("state") == "post" and p.get("hit") is not None:
                settled_picks += 1
                d_settled += 1
                odds_val = float(p.get("odds") or p.get("fair_odds") or 1.35)
                if p["hit"]:
                    hits += 1
                    d_hits += 1
                    profit_u += (odds_val - 1.0)
                else:
                    profit_u -= 1.0
                if idx == 0:
                    lock1_settled += 1
                    if p["hit"]:
                        lock1_hits += 1
        if len(picks) == 3 and d_settled == 3:
            full_settled_days += 1
            if d_hits == 3:
                sweep_days += 1
            if d_hits >= 2:
                two_plus_days += 1

    return {
        "total_days": total_days,
        "settled_picks": settled_picks,
        "hits": hits,
        "hit_rate": round(hits / settled_picks, 4) if settled_picks > 0 else 0.0,
        "lock1_settled": lock1_settled,
        "lock1_hits": lock1_hits,
        "lock1_hit_rate": round(lock1_hits / lock1_settled, 4) if lock1_settled > 0 else 0.0,
        "full_settled_days": full_settled_days,
        "sweep_days": sweep_days,
        "two_plus_days": two_plus_days,
        "profit_units": round(profit_u, 2),
        "roi": round(profit_u / settled_picks, 4) if settled_picks > 0 else 0.0,
    }


def daily_safe_picks(force_refresh: bool = False) -> dict:
    """Build a 3-to-4 month Daily Calendar of the Top 3 Safest Bets per day across Football, NBA & Tennis."""
    global _daily_picks_cache, _daily_picks_cache_time
    with _lock:
        if not force_refresh and _daily_picks_cache and (time.time() - _daily_picks_cache_time < 600):
            return _daily_picks_cache
        if not force_refresh and not _daily_picks_cache and DAILY_PICKS_FILE.exists():
            try:
                if time.time() - DAILY_PICKS_FILE.stat().st_mtime < 3600:
                    _daily_picks_cache = json.loads(DAILY_PICKS_FILE.read_text(encoding="utf-8"))
                    _daily_picks_cache_time = time.time()
                    return _daily_picks_cache
            except Exception:
                pass

    min_date_str = "2026-08-01"
    by_day: dict[str, list[dict]] = {}
    seen_match_keys: set[tuple] = set()

    # ------------------------------------------------------------------ 1. FOOTBALL
    football.ensure()
    W_fb = weights("football")
    sw_fb = {k: v for k, v in W_fb.items() if k != "market"}

    # 1A. Live, upcoming & recent ESPN fixtures across all 8 leagues
    for lg in C.LEAGUES:
        try:
            lg_fix = football.fixtures(lg, with_finished=True)
        except Exception:
            lg_fix = []
        for f in lg_fix:
            if f.get("error") or not f.get("recommendation"):
                continue
            d_str = (f.get("kickoff") or "")[:10]
            if not d_str or d_str < min_date_str:
                continue
            reco = f["recommendation"]
            p_idx = reco.get("pick_index", 0)
            probs = f.get("probs") or [0.33, 0.34, 0.33]
            # For safe bets, if top outcome is Draw (index 1), evaluate the stronger team win (0 or 2) if >= 52%
            if p_idx == 1:
                p_idx = 0 if probs[0] >= probs[2] else 2
            prob = float(probs[p_idx])
            if prob < 0.48:
                continue

            h_key = (f.get("home_fd") or f.get("home") or "").lower()
            a_key = (f.get("away_fd") or f.get("away") or "").lower()
            m_key = ("football", lg, h_key, a_key, d_str[:7])
            if m_key in seen_match_keys:
                continue
            seen_match_keys.add(m_key)

            odds_arr = f.get("best_odds") or f.get("market_odds") or reco.get("fair_odds") or []
            odds_val = float(odds_arr[p_idx]) if (len(odds_arr) > p_idx and odds_arr[p_idx]) else round(1.0 / max(prob, 0.05), 2)
            if odds_val < 1.06:
                continue

            pick_team = f["home"] if p_idx == 0 else f["away"]
            agree = reco.get("stats_vs_market", {}).get("agree", True)
            conf = "High" if (prob >= 0.60 and agree) else ("Medium" if prob >= 0.48 else "Low")

            state = f.get("state") or "pre"
            hit = None
            score = None
            result = None
            if state == "post" and f.get("home_score") is not None and f.get("away_score") is not None:
                hs, as_ = int(f["home_score"]), int(f["away_score"])
                actual_idx = 0 if hs > as_ else (1 if hs == as_ else 2)
                hit = (p_idx == actual_idx)
                score = f"{hs}-{as_}"
                result = f["home"] if actual_idx == 0 else ("Draw" if actual_idx == 1 else f["away"])

            safety = prob + (0.03 if agree else -0.04) + 0.015
            by_day.setdefault(d_str, []).append({
                "id": f"fb_{f.get('espn_id')}",
                "date": d_str,
                "kickoff": f.get("kickoff"),
                "sport": "football",
                "league": lg,
                "league_name": C.LEAGUES[lg]["name"],
                "match": f"{f['home']} vs {f['away']}",
                "home": f["home"],
                "away": f["away"],
                "home_logo": f.get("home_logo"),
                "away_logo": f.get("away_logo"),
                "pick": f"{pick_team} (Win)",
                "pick_team": pick_team,
                "market_type": "1X2 Match Winner",
                "probability": round(prob, 4),
                "confidence": conf,
                "odds": round(odds_val, 2),
                "fair_odds": round(1.0 / prob, 2),
                "agree": agree,
                "safety_score": round(safety, 5),
                "state": state,
                "score": score,
                "result": result,
                "hit": hit,
            })

    # 1B. Historical current-season matches from football-data.co.uk (August - October 2026)
    if football.history is not None:
        cur_fb = football.history[football.history.season == C.CURRENT_SEASON]
        for r in cur_fb.itertuples(index=False):
            d_str = str(r.date)[:10]
            if d_str < min_date_str:
                continue
            m_key = ("football", r.league, str(r.home).lower(), str(r.away).lower(), d_str[:7])
            if m_key in seen_match_keys:
                continue
            pred = football.prematch_preds.get((r.league, r.home, r.away))
            if not pred:
                continue
            seen_match_keys.add(m_key)

            comps = dict(pred["components"])
            m_odds = [r.oh_avg, r.od_avg, r.oa_avg] if not pd.isna(r.oh_avg) else None
            b_odds = [r.oh_max, r.od_max, r.oa_max] if not pd.isna(r.oh_max) else m_odds
            if m_odds:
                fair = devig(m_odds)
                if fair:
                    comps["market"] = {"p": fair, "odds": [float(o) for o in m_odds]}
            p_final, _ = fb_blend(comps, W_fb)
            p_stats, _ = fb_blend({k: v for k, v in comps.items() if k != "market"}, sw_fb)
            p_mkt = comps.get("market", {}).get("p")
            reco = recommend(p_final, LABELS_1X2, b_odds, m_odds, p_stats, p_mkt)

            p_idx = reco.get("pick_index", 0)
            if p_idx == 1:
                p_idx = 0 if p_final[0] >= p_final[2] else 2
            prob = float(p_final[p_idx])
            if prob < 0.48:
                continue

            odds_arr = b_odds or m_odds or reco.get("fair_odds") or []
            odds_val = float(odds_arr[p_idx]) if (len(odds_arr) > p_idx and odds_arr[p_idx] and not pd.isna(odds_arr[p_idx])) else round(1.0 / max(prob, 0.05), 2)
            if odds_val < 1.06:
                continue

            pick_team = r.home if p_idx == 0 else r.away
            agree = reco.get("stats_vs_market", {}).get("agree", True)
            conf = "High" if (prob >= 0.60 and agree) else ("Medium" if prob >= 0.48 else "Low")

            hs, as_ = int(r.hg), int(r.ag)
            actual_idx = 0 if hs > as_ else (1 if hs == as_ else 2)
            hit = (p_idx == actual_idx)
            result = r.home if actual_idx == 0 else ("Draw" if actual_idx == 1 else r.away)
            safety = prob + (0.03 if agree else -0.04) + 0.015

            by_day.setdefault(d_str, []).append({
                "id": f"fb_hist_{r.league}_{d_str}_{r.home}",
                "date": d_str,
                "kickoff": f"{d_str}T15:00Z",
                "sport": "football",
                "league": r.league,
                "league_name": C.LEAGUES.get(r.league, {}).get("name", r.league),
                "match": f"{r.home} vs {r.away}",
                "home": r.home,
                "away": r.away,
                "home_logo": None,
                "away_logo": None,
                "pick": f"{pick_team} (Win)",
                "pick_team": pick_team,
                "market_type": "1X2 Match Winner",
                "probability": round(prob, 4),
                "confidence": conf,
                "odds": round(odds_val, 2),
                "fair_odds": round(1.0 / prob, 2),
                "agree": agree,
                "safety_score": round(safety, 5),
                "state": "post",
                "score": f"{hs}-{as_}",
                "result": result,
                "hit": hit,
            })

    # ------------------------------------------------------------------ 2. NBA
    try:
        nba_all = nba.all_season_games()
    except Exception:
        nba_all = []
    for g in nba_all:
        reco = g.get("recommendation") or {}
        p_idx = reco.get("pick_index", 0)
        probs = g.get("probs") or [0.5, 0.5]
        prob = float(probs[p_idx])
        if prob < 0.53:
            continue

        ts_kick = pd.Timestamp(g["kickoff"])
        d_str = (ts_kick - pd.Timedelta(hours=5)).strftime("%Y-%m-%d")
        if d_str < min_date_str:
            continue

        odds_arr = g.get("best_odds") or g.get("market_odds") or reco.get("fair_odds") or []
        odds_val = float(odds_arr[p_idx]) if (len(odds_arr) > p_idx and odds_arr[p_idx]) else round(1.0 / max(prob, 0.05), 2)
        if odds_val < 1.06:
            continue

        pick_team = g["home"] if p_idx == 0 else g["away"]
        agree = reco.get("stats_vs_market", {}).get("agree", True)
        conf = reco.get("confidence") or ("High" if prob >= 0.65 else ("Medium" if prob >= 0.56 else "Low"))

        state = g.get("state") or "pre"
        hit = None
        score = None
        result = None
        if state == "post" and g.get("home_score") is not None and g.get("away_score") is not None:
            hs, as_ = int(g["home_score"]), int(g["away_score"])
            actual_idx = 0 if hs > as_ else 1
            hit = (p_idx == actual_idx)
            score = f"{hs}-{as_}"
            result = g["home"] if actual_idx == 0 else g["away"]

        safety = prob + (0.03 if agree else -0.04) + 0.015
        by_day.setdefault(d_str, []).append({
            "id": f"nba_{g.get('espn_id')}",
            "date": d_str,
            "kickoff": g.get("kickoff"),
            "sport": "nba",
            "league": "NBA",
            "league_name": "NBA",
            "match": f"{g['home']} vs {g['away']}",
            "home": g["home"],
            "away": g["away"],
            "home_logo": g.get("home_logo"),
            "away_logo": g.get("away_logo"),
            "pick": f"{pick_team} (ML)",
            "pick_team": pick_team,
            "market_type": "Moneyline",
            "probability": round(prob, 4),
            "confidence": conf,
            "odds": round(odds_val, 2),
            "fair_odds": round(1.0 / prob, 2),
            "agree": agree,
            "safety_score": round(safety, 5),
            "state": state,
            "score": score,
            "result": result,
            "hit": hit,
        })

    # ------------------------------------------------------------------ 3. TENNIS
    tennis.ensure()
    W_tn = weights("tennis")
    sw_tn = {k: v for k, v in W_tn.items() if k != "market"}
    try:
        tn_live = tennis.matches()
    except Exception:
        tn_live = []

    for m in tn_live:
        if m.get("error") or m.get("unknown_players") or m.get("a") == "TBD" or m.get("b") == "TBD":
            continue
        d_str = (m.get("start") or "")[:10]
        if not d_str or d_str < min_date_str:
            continue
        reco = m.get("recommendation") or {}
        p_idx = reco.get("pick_index", 0)
        probs = m.get("probs") or [0.5, 0.5]
        prob = float(probs[p_idx])
        if prob < 0.56:
            continue

        pa_last = m["a"].split()[-1].lower()
        pb_last = m["b"].split()[-1].lower()
        m_key = ("tennis", d_str[:7], frozenset({pa_last, pb_last}))
        if m_key in seen_match_keys:
            continue
        seen_match_keys.add(m_key)

        odds_arr = m.get("best_odds") or m.get("market_odds") or reco.get("fair_odds") or []
        odds_val = float(odds_arr[p_idx]) if (len(odds_arr) > p_idx and odds_arr[p_idx]) else round(1.0 / max(prob, 0.05), 2)
        if odds_val < 1.08:
            continue

        pick_player = m["a"] if p_idx == 0 else m["b"]
        agree = reco.get("stats_vs_market", {}).get("agree", True)
        conf = reco.get("confidence") or ("High" if prob >= 0.65 else "Medium")

        state = m.get("state") or "pre"
        hit = None
        score = m.get("score") or None
        result = None
        if state == "post" and (m.get("a_winner") or m.get("b_winner")):
            actual_idx = 0 if m.get("a_winner") else 1
            hit = (p_idx == actual_idx)
            result = m["a"] if actual_idx == 0 else m["b"]

        tier = m.get("tier") or "ATP/WTA"
        tier_bonus = 0.012 if any(k in tier for k in ("Grand Slam", "1000", "Finals", "500")) else 0.0
        safety = prob + (0.03 if agree else -0.04) + tier_bonus

        by_day.setdefault(d_str, []).append({
            "id": f"tn_{m.get('espn_id')}",
            "date": d_str,
            "kickoff": m.get("start"),
            "sport": "tennis",
            "league": m.get("tour") or "ATP",
            "league_name": f"{m.get('tour', 'ATP')} • {m.get('tournament', 'Tour')}",
            "match": f"{m['a']} vs {m['b']}",
            "home": m["a"],
            "away": m["b"],
            "home_logo": m.get("a_flag"),
            "away_logo": m.get("b_flag"),
            "pick": f"{pick_player} (Win)",
            "pick_team": pick_player,
            "market_type": "Match Winner",
            "probability": round(prob, 4),
            "confidence": conf,
            "odds": round(odds_val, 2),
            "fair_odds": round(1.0 / prob, 2),
            "agree": agree,
            "safety_score": round(safety, 5),
            "state": state,
            "score": score,
            "result": result,
            "hit": hit,
        })

    # 3B. Historical 2026 ATP/WTA matches from tennisdata (August - October 2026)
    for hp in tennis.recent_hist_preds:
        d_str = hp["date"]
        if d_str < min_date_str:
            continue
        tier = tennis._tier(hp["tour"], hp["tournament"])
        if "125" in tier or "Challenger" in tier:
            continue
        pa_last = str(hp["a"]).split()[-1].lower()
        pb_last = str(hp["b"]).split()[-1].lower()
        m_key = ("tennis", d_str[:7], frozenset({pa_last, pb_last}))
        if m_key in seen_match_keys:
            continue

        comps = hp["components"]
        pa, _ = tn_blend(comps, W_tn)
        ps, _ = tn_blend({k: v for k, v in comps.items() if k != "market"}, sw_tn)
        pm = comps.get("market", {}).get("p")
        o_avg = hp.get("odds_avg")
        o_max = hp.get("odds_max") or o_avg
        valid_avg = o_avg if (o_avg and all(x and not pd.isna(x) for x in o_avg)) else None
        valid_max = o_max if (o_max and all(x and not pd.isna(x) for x in o_max)) else valid_avg
        if not valid_avg:
            continue

        seen_match_keys.add(m_key)
        reco = recommend([pa, 1.0 - pa], [hp["a"], hp["b"]], valid_max, valid_avg, [ps, 1.0 - ps], [pm, 1.0 - pm] if pm is not None else None)
        p_idx = reco["pick_index"]
        prob = float(reco["probability"])
        if prob < 0.56:
            continue

        odds_val = float(valid_max[p_idx]) if valid_max else round(1.0 / max(prob, 0.05), 2)
        if odds_val < 1.08:
            continue

        pick_player = hp["a"] if p_idx == 0 else hp["b"]
        agree = reco.get("stats_vs_market", {}).get("agree", True)
        actual_idx = 0 if hp["a_is_w"] else 1
        hit = (p_idx == actual_idx)
        result = hp["a"] if actual_idx == 0 else hp["b"]
        tier_bonus = 0.012 if any(k in tier for k in ("Grand Slam", "1000", "Finals", "500")) else 0.0
        safety = prob + (0.03 if agree else -0.04) + tier_bonus

        by_day.setdefault(d_str, []).append({
            "id": f"tn_hist_{d_str}_{hp['a']}_{hp['b']}",
            "date": d_str,
            "kickoff": f"{d_str}T12:00Z",
            "sport": "tennis",
            "league": hp["tour"],
            "league_name": f"{hp['tour']} • {hp['tournament']}",
            "match": f"{hp['a']} vs {hp['b']}",
            "home": hp["a"],
            "away": hp["b"],
            "home_logo": None,
            "away_logo": None,
            "pick": f"{pick_player} (Win)",
            "pick_team": pick_player,
            "market_type": "Match Winner",
            "probability": round(prob, 4),
            "confidence": reco["confidence"],
            "odds": round(odds_val, 2),
            "fair_odds": round(1.0 / prob, 2),
            "agree": agree,
            "safety_score": round(safety, 5),
            "state": "post",
            "score": hp["score"],
            "result": result,
            "hit": hit,
        })

    # ------------------------------------------------------------------ 4. SELECT TOP 3 PER DAY
    days_out: dict[str, dict] = {}
    months_set: set[str] = set()

    for d_str in sorted(by_day.keys()):
        cands = by_day[d_str]
        # Preferplayable odds >= 1.12 first; fallback to all if < 3
        playable = [c for c in cands if c["odds"] >= 1.12]
        pool = playable if len(playable) >= 3 else cands
        pool_sorted = sorted(pool, key=lambda x: (x["safety_score"], x["probability"]), reverse=True)

        top3 = []
        for rank_i, item in enumerate(pool_sorted[:3], start=1):
            top3.append({**item, "rank": rank_i})

        by_sport = {}
        for sp in ("football", "nba", "tennis"):
            sp_cands = [c for c in cands if c["sport"] == sp and c["odds"] >= 1.10]
            if len(sp_cands) < 3:
                sp_cands = [c for c in cands if c["sport"] == sp]
            sp_sorted = sorted(sp_cands, key=lambda x: (x["safety_score"], x["probability"]), reverse=True)
            by_sport[sp] = [{**item, "rank": ri} for ri, item in enumerate(sp_sorted[:3], start=1)]

        settled_c = sum(1 for p in top3 if p["state"] == "post" and p["hit"] is not None)
        hits_c = sum(1 for p in top3 if p["state"] == "post" and p["hit"] is True)
        day_prof = 0.0
        comb_odds = 1.0
        for p in top3:
            comb_odds *= float(p["odds"])
            if p["state"] == "post" and p["hit"] is not None:
                day_prof += (float(p["odds"]) - 1.0) if p["hit"] else -1.0

        ym = d_str[:7]
        months_set.add(ym)
        days_out[d_str] = {
            "date": d_str,
            "month": ym,
            "top3": top3,
            "by_sport": by_sport,
            "total_candidates": len(cands),
            "settled_count": settled_c,
            "hits_count": hits_c,
            "sweep": (len(top3) == 3 and settled_c == 3 and hits_c == 3),
            "day_profit_u": round(day_prof, 2),
            "combined_odds": round(comb_odds, 2) if top3 else 0.0,
        }

    months_sorted = sorted(months_set, reverse=True)
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    res = {
        "today": today_iso,
        "months": months_sorted,
        "summary": _compute_summary(days_out, None),
        "summary_by_sport": {
            "football": _compute_summary(days_out, "football"),
            "nba": _compute_summary(days_out, "nba"),
            "tennis": _compute_summary(days_out, "tennis"),
        },
        "days": days_out,
    }
    with _lock:
        _daily_picks_cache = res
        _daily_picks_cache_time = time.time()
        try:
            DAILY_PICKS_FILE.parent.mkdir(parents=True, exist_ok=True)
            DAILY_PICKS_FILE.write_text(json.dumps(res, indent=0), encoding="utf-8")
        except Exception:
            pass
    return res


def warmup_all() -> dict:
    """Warm up engines sequentially with garbage collection so cloud containers stay under 512MB RAM."""
    global _warmup_running
    if _warmup_running:
        return {"status": "already_warming"}
    _warmup_running = True
    try:
        football.ensure()
        gc.collect()
        tennis.ensure()
        gc.collect()
        for lg in ("EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1", "UCL"):
            try:
                football.fixtures(lg, with_finished=True)
            except Exception:
                pass
        gc.collect()
        try:
            nba.all_season_games()
        except Exception:
            pass
        gc.collect()
        try:
            tennis.matches()
        except Exception:
            pass
        try:
            daily_safe_picks()
        except Exception:
            pass
        gc.collect()
        return {"status": "ready"}
    finally:
        _warmup_running = False


def track_record() -> dict:
    t = _track_load()
    done = [v for v in t.values() if "result" in v]
    hits = sum(1 for v in done if v["pick"] == v["result"])
    profit = 0.0
    nb = 0
    for v in done:
        for vb in v.get("value") or []:
            idx = LABELS_1X2.index(vb["outcome"]) if vb["outcome"] in LABELS_1X2 else None
            if idx is None:
                continue
            nb += 1
            profit += (vb["odds"] - 1) if idx == v["result"] else -1
    return {"tracked": len(t), "settled": len(done), "pick_hit_rate": round(hits / len(done), 3) if done else None,
            "value_bets": nb, "value_profit_units": round(profit, 2),
            "recent": sorted(done, key=lambda v: v["kickoff"], reverse=True)[:50]}
