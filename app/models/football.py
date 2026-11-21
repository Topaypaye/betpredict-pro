"""Football prediction engine.

The engine is *online*: it walks through matches in chronological order and
keeps ratings that only use information available before kick-off. This lets
the exact same code power live predictions and leak-free back-tests.

Components (each returns H/D/A probabilities):
  market        de-vigged bookmaker odds
  xg_poisson    Dixon-Coles Poisson on opponent-adjusted, time-weighted xG
  goals_poisson same on real goals
  elo           Elo rating difference + home advantage
  form          last-5 points/game and xG difference
  table         season PPG and goal difference (shrunk early season)
  h2h           head-to-head record shrunk toward league base rates
Adjustments (applied to Poisson expected goals): injuries, promoted, rest.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import FOOTBALL_ADJ, FOOTBALL_PARAMS
from .common import devig, elo_to_1x2, matrix_markets, score_matrix

COMPONENTS = ["market", "xg_poisson", "goals_poisson", "elo", "form", "table", "h2h"]


@dataclass
class TeamState:
    elo: float = 1500.0
    att_xg: float = 1.35
    def_xg: float = 1.35
    att_g: float = 1.35
    def_g: float = 1.35
    home_att_xg: float = 1.35
    home_def_xg: float = 1.35
    away_att_xg: float = 1.35
    away_def_xg: float = 1.35
    n: int = 0
    season: str | None = None
    s_games: int = 0
    s_pts: int = 0
    s_gd: int = 0
    promoted: bool = False
    last_date: pd.Timestamp | None = None
    recent: deque = field(default_factory=lambda: deque(maxlen=10))  # (pts, xgd, gd, result_char)


@dataclass
class LeagueState:
    mu_g: float = 1.35
    mu_xg: float = 1.35
    home_g: float = 1.50
    away_g: float = 1.20
    home_xg: float = 1.50
    away_xg: float = 1.20
    draw: float = 0.26
    base_h: float = 0.44
    base_a: float = 0.30
    season: str | None = None
    teams: dict = field(default_factory=lambda: defaultdict(set))   # season -> set(team)


class FootballEngine:
    def __init__(self, params: dict | None = None, adj: dict | None = None):
        self.p = {**FOOTBALL_PARAMS, **(params or {})}
        self.adj = {**FOOTBALL_ADJ, **(adj or {})}
        self.teams: dict[tuple, TeamState] = {}
        self.leagues: dict[str, LeagueState] = defaultdict(LeagueState)
        self.h2h: dict[tuple, list] = defaultdict(list)

    # ------------------------------------------------------------------ state
    def _prev_season_teams(self, lg: LeagueState, season: str) -> set:
        older = sorted(s for s in lg.teams if s < season)
        return lg.teams[older[-1]] if older else set()

    def _team(self, league: str, team: str, season: str) -> TeamState:
        lg = self.leagues[league]
        key = (league, team)
        ts = self.teams.get(key)
        if ts is not None and ts.season == season:
            return ts
        prev = self._prev_season_teams(lg, season)
        promoted = bool(prev) and team not in prev
        if ts is None or promoted:
            # Check if this team is known in another league (e.g. UCL/UEL match for domestic club)
            existing = None
            if ts is None:
                for (l, t), candidate in self.teams.items():
                    if t == team and candidate.n > 0:
                        existing = candidate
                        break
            if existing is not None:
                import copy
                ts = copy.deepcopy(existing)
                ts.season = season
                self.teams[key] = ts
                lg.teams[season].add(team)
                return ts

            # New in the data set, or promoted: start from league priors.
            elos = [self.teams[(league, t)].elo for t in prev if (league, t) in self.teams]
            mean_elo = float(np.mean(elos)) if elos else 1500.0
            ts = TeamState()
            if promoted:
                ts.elo = mean_elo + self.p["elo_promoted_offset"]
                a, d = self.adj["promoted_attack"], self.adj["promoted_defence"]
            else:
                ts.elo = mean_elo
                a, d = 1.0, 1.0
            ts.att_xg, ts.def_xg = lg.mu_xg * a, lg.mu_xg * d
            ts.home_att_xg, ts.home_def_xg = ts.att_xg, ts.def_xg
            ts.away_att_xg, ts.away_def_xg = ts.att_xg, ts.def_xg
            ts.att_g, ts.def_g = lg.mu_g * a, lg.mu_g * d
            self.teams[key] = ts
        else:
            # Season rollover: regress Elo to the mean.
            r = self.p["elo_season_regress"]
            ts.elo = ts.elo * (1 - r) + 1500.0 * r
        ts.season, ts.s_games, ts.s_pts, ts.s_gd, ts.promoted = season, 0, 0, 0, promoted
        lg.teams[season].add(team)
        return ts

    # ------------------------------------------------------------ components
    def _lambdas(self, lg, th, ta, kind):
        if kind == "xg":
            mu, hv, av = lg.mu_xg, lg.home_xg, lg.away_xg
            # Blend 72% overall rating + 28% venue-specific (Home-at-Home / Away-at-Away) rating
            ah = 0.72 * th.att_xg + 0.28 * th.home_att_xg
            dh = 0.72 * th.def_xg + 0.28 * th.home_def_xg
            aa = 0.72 * ta.att_xg + 0.28 * ta.away_att_xg
            da = 0.72 * ta.def_xg + 0.28 * ta.away_def_xg
        else:
            mu, hv, av = lg.mu_g, lg.home_g, lg.away_g
            ah, dh, aa, da = th.att_g, th.def_g, ta.att_g, ta.def_g
        lh = hv * (ah / mu) * (da / mu)
        la = av * (aa / mu) * (dh / mu)
        return lh, la

    def _form(self, ts: TeamState):
        rec = list(ts.recent)[-self.p["form_n"]:]
        if not rec:
            return 1.35, 0.0, ""
        ppg = sum(r[0] for r in rec) / len(rec)
        xgd = sum(r[1] for r in rec) / len(rec)
        return ppg, xgd, "".join(r[3] for r in rec)

    def _table(self, ts: TeamState):
        k = self.p["table_prior_games"]
        ppg = (ts.s_pts + 1.37 * k) / (ts.s_games + k)
        gd = ts.s_gd / (ts.s_games + k)
        return ppg, gd

    def _h2h(self, league, home, away, date):
        lg = self.leagues[league]
        games = self.h2h[(league, *sorted((home, away)))]
        cutoff = date - pd.Timedelta(days=365 * self.p["h2h_years"])
        games = [g for g in games if g[0] >= cutoff][-self.p["h2h_n"]:]
        hw = sum(1 for g in games if g[3] == home)
        dr = sum(1 for g in games if g[3] == "D")
        aw = sum(1 for g in games if g[3] == away)
        k = self.p["h2h_prior"]
        base = [lg.base_h, lg.draw, lg.base_a]
        n = len(games)
        p = [(hw + k * base[0]) / (n + k), (dr + k * base[1]) / (n + k), (aw + k * base[2]) / (n + k)]
        s = sum(p)
        return [x / s for x in p], {"n": n, "home_wins": hw, "draws": dr, "away_wins": aw}

    def predict(self, league, season, date, home, away, odds=None, live_adj=None) -> dict:
        """Return {"components": {name: {...}}, "features": {...}} for a fixture."""
        date = pd.Timestamp(date)
        lg = self.leagues[league]
        th = self._team(league, home, season)
        ta = self._team(league, away, season)
        P = self.p
        comps: dict[str, dict] = {}

        # --- adjustments on expected goals --------------------------------
        mh, ma, adj_notes = 1.0, 1.0, []
        live_adj = live_adj or {}
        for side, key in (("home", "home_out"), ("away", "away_out")):
            n_out = live_adj.get(key, 0) or 0
            if n_out:
                pen = min(n_out * self.adj["injury_per_key_player"], self.adj["injury_cap"])
                if side == "home":
                    mh *= 1 - pen
                else:
                    ma *= 1 - pen
                adj_notes.append(f"{side}: {n_out} key absentee(s) -> -{pen:.0%} xG")
        rest_h = (date - th.last_date).days if th.last_date is not None else None
        rest_a = (date - ta.last_date).days if ta.last_date is not None else None
        lim = self.adj["rest_short_days"]
        if rest_h is not None and rest_a is not None:
            if rest_h <= lim < rest_a:
                mh *= 1 - self.adj["rest_penalty"]; adj_notes.append(f"home short rest ({rest_h}d)")
            elif rest_a <= lim < rest_h:
                ma *= 1 - self.adj["rest_penalty"]; adj_notes.append(f"away short rest ({rest_a}d)")

        # --- market ---------------------------------------------------------
        if odds is not None:
            fair = devig(odds)
            if fair:
                comps["market"] = {"p": fair, "odds": [float(o) for o in odds]}

        # --- Poisson models -------------------------------------------------
        for kind, name in (("xg", "xg_poisson"), ("g", "goals_poisson")):
            lh, la = self._lambdas(lg, th, ta, kind)
            lh, la = lh * mh, la * ma
            mk = matrix_markets(score_matrix(lh, la, P["dc_rho"]))
            comps[name] = {"p": mk["1x2"], "lambda": [round(lh, 2), round(la, 2)],
                           "over25": mk["over25"], "btts": mk["btts"], "top_score": mk["top_score"]}

        # --- Elo ------------------------------------------------------------
        diff = th.elo + P["elo_hfa"] - ta.elo
        comps["elo"] = {"p": elo_to_1x2(diff, lg.draw), "diff": round(diff, 1)}

        # --- form -----------------------------------------------------------
        fh, fa = self._form(th), self._form(ta)
        fdiff = P["form_scale_ppg"] * (fh[0] - fa[0]) + P["form_scale_xgd"] * (fh[1] - fa[1]) + P["elo_hfa"]
        comps["form"] = {"p": elo_to_1x2(fdiff, lg.draw), "diff": round(fdiff, 1)}

        # --- table ----------------------------------------------------------
        tbh, tba = self._table(th), self._table(ta)
        tdiff = P["table_scale_ppg"] * (tbh[0] - tba[0]) + P["table_scale_gd"] * (tbh[1] - tba[1]) + P["elo_hfa"]
        comps["table"] = {"p": elo_to_1x2(tdiff, lg.draw), "diff": round(tdiff, 1)}

        # --- h2h ------------------------------------------------------------
        hp, hinfo = self._h2h(league, home, away, date)
        comps["h2h"] = {"p": hp, **hinfo}

        features = {
            "elo": [round(th.elo), round(ta.elo)],
            "xg_for_pg": [round(th.att_xg, 2), round(ta.att_xg, 2)],
            "xg_against_pg": [round(th.def_xg, 2), round(ta.def_xg, 2)],
            "goals_for_pg": [round(th.att_g, 2), round(ta.att_g, 2)],
            "goals_against_pg": [round(th.def_g, 2), round(ta.def_g, 2)],
            "form": [fh[2], fa[2]],
            "form_ppg": [round(fh[0], 2), round(fa[0], 2)],
            "form_xgd": [round(fh[1], 2), round(fa[1], 2)],
            "season_ppg": [round(th.s_pts / th.s_games, 2) if th.s_games else None,
                           round(ta.s_pts / ta.s_games, 2) if ta.s_games else None],
            "season_gd": [th.s_gd, ta.s_gd],
            "season_games": [th.s_games, ta.s_games],
            "promoted": [th.promoted, ta.promoted],
            "rest_days": [rest_h, rest_a],
            "adjustments": adj_notes,
            "league_draw_rate": round(lg.draw, 3),
        }
        return {"components": comps, "features": features}

    # --------------------------------------------------------------- update
    def update(self, row) -> None:
        league, season, date = row.league, row.season, pd.Timestamp(row.date)
        home, away = row.home, row.away
        hg, ag, hxg, axg = float(row.hg), float(row.ag), float(row.hxg), float(row.axg)
        lg = self.leagues[league]
        th, ta = self._team(league, home, season), self._team(league, away, season)
        P = self.p
        # Red-card anomaly damping: prevent 10-man blowouts from corrupting 11v11 ratings
        hr = getattr(row, "hr", 0)
        ar = getattr(row, "ar", 0)
        red_card = (not pd.isna(hr) and float(hr) > 0) or (not pd.isna(ar) and float(ar) > 0)
        a = P["ewma_alpha"] * (0.40 if red_card else 1.0)
        elo_k = P["elo_k"] * (0.50 if red_card else 1.0)
        va = 0.04 if red_card else 0.10

        # Venue-specific attack/defense EWMA updates
        th.home_att_xg = (1 - va) * th.home_att_xg + va * hxg
        th.home_def_xg = (1 - va) * th.home_def_xg + va * axg
        ta.away_att_xg = (1 - va) * ta.away_att_xg + va * axg
        ta.away_def_xg = (1 - va) * ta.away_def_xg + va * hxg

        # opponent/venue-adjusted rating updates (online Dixon-Coles style)
        for kind, gh, ga in (("xg", hxg, axg), ("g", hg, ag)):
            mu = lg.mu_xg if kind == "xg" else lg.mu_g
            hv = lg.home_xg if kind == "xg" else lg.home_g
            av = lg.away_xg if kind == "xg" else lg.away_g
            att_h, def_h = getattr(th, f"att_{kind}"), getattr(th, f"def_{kind}")
            att_a, def_a = getattr(ta, f"att_{kind}"), getattr(ta, f"def_{kind}")
            clip = lambda x: min(max(x, 0.5), 2.0)  # noqa: E731
            obs_att_h = gh / (hv / mu) / clip(def_a / mu)
            obs_def_a = gh / (hv / mu) / clip(att_h / mu)
            obs_att_a = ga / (av / mu) / clip(def_h / mu)
            obs_def_h = ga / (av / mu) / clip(att_a / mu)
            setattr(th, f"att_{kind}", (1 - a) * att_h + a * obs_att_h)
            setattr(th, f"def_{kind}", (1 - a) * def_h + a * obs_def_h)
            setattr(ta, f"att_{kind}", (1 - a) * att_a + a * obs_att_a)
            setattr(ta, f"def_{kind}", (1 - a) * def_a + a * obs_def_a)

        # league averages (slow EWMA)
        b = 0.004
        lg.home_g = (1 - b) * lg.home_g + b * hg
        lg.away_g = (1 - b) * lg.away_g + b * ag
        lg.home_xg = (1 - b) * lg.home_xg + b * hxg
        lg.away_xg = (1 - b) * lg.away_xg + b * axg
        lg.mu_g = (lg.home_g + lg.away_g) / 2
        lg.mu_xg = (lg.home_xg + lg.away_xg) / 2
        res = "H" if hg > ag else ("A" if ag > hg else "D")
        lg.draw = (1 - b) * lg.draw + b * (res == "D")
        lg.base_h = (1 - b) * lg.base_h + b * (res == "H")
        lg.base_a = (1 - b) * lg.base_a + b * (res == "A")

        # Elo with goal-difference multiplier
        diff = th.elo + P["elo_hfa"] - ta.elo
        exp_h = 1.0 / (1.0 + 10 ** (-diff / 400.0))
        s_h = 1.0 if res == "H" else (0.5 if res == "D" else 0.0)
        gd = abs(hg - ag)
        mult = 1.0 if gd <= 1 else (1.5 if gd == 2 else (11 + gd) / 8)
        delta = elo_k * mult * (s_h - exp_h)
        th.elo += delta
        ta.elo -= delta

        # form / table / misc
        pts_h = 3 if res == "H" else (1 if res == "D" else 0)
        pts_a = 3 if res == "A" else (1 if res == "D" else 0)
        ch = {"H": "W", "D": "D", "A": "L"}[res]
        ca = {"H": "L", "D": "D", "A": "W"}[res]
        th.recent.append((pts_h, hxg - axg, hg - ag, ch))
        ta.recent.append((pts_a, axg - hxg, ag - hg, ca))
        for ts, pts, g in ((th, pts_h, hg - ag), (ta, pts_a, ag - hg)):
            ts.s_games += 1; ts.s_pts += pts; ts.s_gd += int(g); ts.n += 1; ts.last_date = date
        self.h2h[(league, *sorted((home, away)))].append((date, home, away, res if res == "D" else (home if res == "H" else away)))

    # ------------------------------------------------------------- helpers
    def run(self, df: pd.DataFrame, callback=None, odds_cols=("oh_avg", "od_avg", "oa_avg"),
            callback_season: str | None = None) -> None:
        """Process matches chronologically. If callback(row, prediction) is given
        it is called *before* each match updates the ratings (leak-free)."""
        for row in df.itertuples(index=False):
            if pd.isna(row.hg) or pd.isna(row.ag):
                continue
            if callback is not None and (callback_season is None or row.season == callback_season):
                # Build sharpest consensus pre-match/closing odds from Pinnacle & Market Avg when available
                o_avg = [getattr(row, c, np.nan) for c in odds_cols]
                o_cavg = [getattr(row, c, np.nan) for c in ("oh_cavg", "od_cavg", "oa_cavg")]
                o_ps = [getattr(row, c, np.nan) for c in ("oh_ps", "od_ps", "oa_ps")]
                o_cps = [getattr(row, c, np.nan) for c in ("oh_cps", "od_cps", "oa_cps")]
                valid_sets = []
                weights_sets = []
                for o_set, w_set in ((o_cps, 0.30), (o_cavg, 0.25), (o_ps, 0.20), (o_avg, 0.25)):
                    if all(not pd.isna(x) and float(x) > 1.0 for x in o_set):
                        valid_sets.append(np.array(o_set, dtype=float))
                        weights_sets.append(w_set)
                if valid_sets:
                    w_arr = np.array(weights_sets) / sum(weights_sets)
                    odds = np.sum([v * w for v, w in zip(valid_sets, w_arr)], axis=0).tolist()
                else:
                    odds = None
                callback(row, self.predict(row.league, row.season, row.date, row.home, row.away, odds))
            self.update(row)

    def teams_in(self, league: str, season: str) -> list[str]:
        return sorted(self.leagues[league].teams.get(season, set()))


# Marginal base-rate de-biasing factors (removes EWMA shrinkage Home-Bias in slight-Away matchups)
STAT_DEBIAS = np.array([0.963, 1.037, 1.025])


def blend(components: dict, weights: dict) -> tuple[list[float], dict]:
    """Conviction-gated opinion pool with statistical Home-Bias correction."""
    used = {k: weights.get(k, 0.0) for k in components if weights.get(k, 0.0) > 0}
    tot = sum(used.values())
    if tot <= 0:
        return [1 / 3] * 3, {}
    norm_w = {k: w / tot for k, w in used.items()}

    # Compute de-biased pure statistical blend
    stat_keys = [k for k in norm_w if k != "market"]
    stat_tot = sum(norm_w[k] for k in stat_keys)
    if "market" in norm_w and stat_tot > 0:
        ms = norm_w["market"]
        p_stat = np.zeros(3)
        for k in stat_keys:
            p_stat += (norm_w[k] / stat_tot) * np.asarray(components[k]["p"], dtype=float)
        p_stat = p_stat * STAT_DEBIAS
        p_stat = p_stat / p_stat.sum()
        p_mkt = np.asarray(components["market"]["p"], dtype=float)
        p = ms * p_mkt + (1.0 - ms) * p_stat
        # Conviction gate: only overturn market top pick when statistical conviction edge >= 16%
        if int(p.argmax()) != int(p_mkt.argmax()):
            st_edge = float(p_stat[int(p_stat.argmax())] - p_stat[int(p_mkt.argmax())])
            if st_edge < 0.16:
                p = 0.96 * p_mkt + 0.04 * p_stat
        p = p / p.sum()
        return [float(x) for x in p], norm_w

    p = np.zeros(3)
    for k, w in norm_w.items():
        p += w * np.asarray(components[k]["p"], dtype=float)
    return [float(x) for x in p / p.sum()], norm_w
