"""NBA prediction engine: FiveThirtyEight Elo + Net Rating + Pythagorean + Form + Home/Road Split.

Components (each returns P(Home Team wins)):
  market       de-vigged bookmaker moneyline odds (DraftKings / Bovada / The-Odds-API)
  elo          FiveThirtyEight NBA Elo with Margin-of-Victory multiplier & +70 HCA
  net_rating   Offensive/Defensive PPG efficiency shrunk toward priors -> expected margin & normal CDF
  pythagorean  Morey's Basketball Pythagorean expectation (exponent 13.91) combined via Log5
  form         Last-10 games win rate & streak momentum
  home_road    Home court vs Road court win-rate split

Also produces:
  proj_home_pts, proj_away_pts, proj_total, proj_margin, over_prob (against book O/U line or 226.5).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import NBA_PARAMS
from .common import devig, elo_expect

COMPONENTS = ["market", "elo", "net_rating", "pythagorean", "form", "home_road"]

# Baseline NBA franchise Elo & PPG priors entering 2026-27 (calibrated from full 82-game 2025-26 season)
NBA_BASE_ELO: dict[str, float] = {
    "Oklahoma City Thunder": 1725.3,  # 2025-26: 64-18 (+11.1 diff)
    "San Antonio Spurs": 1685.2,      # 2025-26: 62-20 (+8.3 diff)
    "Detroit Pistons": 1675.3,        # 2025-26: 60-22 (+8.2 diff)
    "Boston Celtics": 1652.1,         # 2025-26: 56-26 (+7.7 diff)
    "New York Knicks": 1624.4,        # 2025-26: 53-29 (+6.4 diff)
    "Denver Nuggets": 1615.3,         # 2025-26: 54-28 (+5.2 diff)
    "Houston Rockets": 1606.5,        # 2025-26: 52-30 (+5.2 diff)
    "Cleveland Cavaliers": 1594.2,    # 2025-26: 52-30 (+4.1 diff)
    "Minnesota Timberwolves": 1573.2, # 2025-26: 49-33 (+3.4 diff)
    "Los Angeles Lakers": 1571.7,     # 2025-26: 53-29 (+1.7 diff)
    "Charlotte Hornets": 1566.9,      # 2025-26: 44-38 (+4.8 diff)
    "Toronto Raptors": 1553.3,        # 2025-26: 46-36 (+2.8 diff)
    "Atlanta Hawks": 1550.0,          # 2025-26: 46-36 (+2.5 diff)
    "Miami Heat": 1535.7,             # 2025-26: 43-39 (+2.4 diff)
    "Phoenix Suns": 1534.4,           # 2025-26: 45-37 (+1.5 diff)
    "Orlando Magic": 1524.3,          # 2025-26: 45-37 (+0.6 diff)
    "LA Clippers": 1517.8,            # 2025-26: 42-40 (+1.2 diff)
    "Philadelphia 76ers": 1515.3,     # 2025-26: 45-37 (-0.2 diff)
    "Portland Trail Blazers": 1501.0, # 2025-26: 42-40 (-0.3 diff)
    "Golden State Warriors": 1475.7,  # 2025-26: 37-45 (-0.6 diff)
    "Chicago Bulls": 1397.9,          # 2025-26: 31-51 (-5.2 diff)
    "Milwaukee Bucks": 1391.0,        # 2025-26: 32-50 (-6.2 diff)
    "New Orleans Pelicans": 1383.7,   # 2025-26: 26-56 (-4.5 diff)
    "Dallas Mavericks": 1372.5,       # 2025-26: 26-56 (-5.5 diff)
    "Memphis Grizzlies": 1362.6,      # 2025-26: 25-57 (-6.0 diff)
    "Utah Jazz": 1322.5,              # 2025-26: 22-60 (-8.4 diff)
    "Indiana Pacers": 1313.8,         # 2025-26: 19-63 (-8.0 diff)
    "Sacramento Kings": 1304.6,       # 2025-26: 22-60 (-10.0 diff)
    "Brooklyn Nets": 1295.8,          # 2025-26: 20-62 (-10.0 diff)
    "Washington Wizards": 1260.2,     # 2025-26: 17-65 (-12.0 diff)
}

NBA_PRIOR_PPG: dict[str, tuple[float, float]] = {
    "Oklahoma City Thunder": (119.0, 107.9),
    "San Antonio Spurs": (119.8, 111.5),
    "Detroit Pistons": (117.8, 109.6),
    "Boston Celtics": (114.9, 107.2),
    "New York Knicks": (116.5, 110.1),
    "Denver Nuggets": (122.1, 116.9),
    "Houston Rockets": (115.2, 110.0),
    "Cleveland Cavaliers": (119.5, 115.4),
    "Minnesota Timberwolves": (118.0, 114.6),
    "Los Angeles Lakers": (116.3, 114.6),
    "Charlotte Hornets": (116.0, 111.2),
    "Toronto Raptors": (114.6, 111.8),
    "Atlanta Hawks": (118.5, 116.0),
    "Miami Heat": (120.9, 118.5),
    "Phoenix Suns": (112.6, 111.1),
    "Orlando Magic": (115.7, 115.1),
    "LA Clippers": (113.8, 112.6),
    "Philadelphia 76ers": (115.9, 116.1),
    "Portland Trail Blazers": (115.5, 115.8),
    "Golden State Warriors": (114.6, 115.2),
    "Chicago Bulls": (116.3, 121.5),
    "Milwaukee Bucks": (110.6, 116.8),
    "New Orleans Pelicans": (115.5, 120.0),
    "Dallas Mavericks": (114.1, 119.6),
    "Memphis Grizzlies": (114.7, 120.7),
    "Utah Jazz": (117.6, 126.0),
    "Indiana Pacers": (112.4, 120.4),
    "Sacramento Kings": (111.0, 121.0),
    "Brooklyn Nets": (105.9, 115.9),
    "Washington Wizards": (112.9, 124.9),
}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _parse_record(rec_str: str | None) -> tuple[int, int]:
    if not rec_str or "-" not in str(rec_str):
        return 0, 0
    try:
        parts = str(rec_str).strip().split("-")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 0, 0


def standings_as_of(results: list[dict], as_of: str) -> tuple[list[dict], dict[str, str]]:
    """Rebuild standings-style rows using ONLY games that finished before `as_of` (ISO kickoff).

    Prevents look-ahead: a finished game is rated with the information available at tip-off.
    Also returns each team's last game kickoff (for rest-day calculation).
    """
    games: dict[str, list[tuple[str, bool, int, int]]] = {}
    for g in results:
        if g["kickoff"] >= as_of:
            continue
        h, a, hs, as_ = g["home"], g["away"], g["home_score"], g["away_score"]
        games.setdefault(h, []).append((g["kickoff"], True, hs, as_))
        games.setdefault(a, []).append((g["kickoff"], False, as_, hs))
    rows, last_game = [], {}
    for team, gl in games.items():
        gl.sort()
        n = len(gl)
        wins = sum(1 for _, _, pf, pa in gl if pf > pa)
        hw = sum(1 for _, home, pf, pa in gl if home and pf > pa)
        hl = sum(1 for _, home, pf, pa in gl if home and pf < pa)
        rw = sum(1 for _, home, pf, pa in gl if not home and pf > pa)
        rl = sum(1 for _, home, pf, pa in gl if not home and pf < pa)
        l10 = gl[-10:]
        l10w = sum(1 for _, _, pf, pa in l10 if pf > pa)
        streak = 0
        for _, _, pf, pa in reversed(gl):
            won = pf > pa
            if streak == 0:
                streak = 1 if won else -1
            elif (streak > 0) == won:
                streak += 1 if won else -1
            else:
                break
        rows.append({
            "team": team, "wins": wins, "losses": n - wins,
            "ppg_for": sum(x[2] for x in gl) / n, "ppg_against": sum(x[3] for x in gl) / n,
            "home_record": f"{hw}-{hl}", "road_record": f"{rw}-{rl}",
            "last10": f"{l10w}-{len(l10) - l10w}", "streak": streak,
        })
        last_game[team] = gl[-1][0]
    return rows, last_game


@dataclass
class NBATeamState:
    name: str
    elo: float = 1500.0
    wins: int = 0
    losses: int = 0
    ppg_for: float = 114.5
    ppg_against: float = 114.5
    pace: float = 99.5
    ortg: float = 115.0
    drtg: float = 115.0
    home_record: str = "0-0"
    road_record: str = "0-0"
    last10_record: str = "5-5"
    streak: int = 0
    last_game_date: str | None = None


class NBAEngine:
    def __init__(self, params: dict | None = None):
        self.p = {**NBA_PARAMS, **(params or {})}
        self.teams: dict[str, NBATeamState] = {}
        self._init_priors()

    def _init_priors(self):
        avg_ppg = self.p["league_avg_ppg"]
        reg_elo = self.p.get("elo_season_regress", 0.35)
        reg_ppg = self.p.get("ppg_season_regress", 0.35)
        for name, base_elo in NBA_BASE_ELO.items():
            raw_pf, raw_pa = NBA_PRIOR_PPG.get(name, (avg_ppg, avg_ppg))
            prior_pf = (1.0 - reg_ppg) * raw_pf + reg_ppg * avg_ppg
            prior_pa = (1.0 - reg_ppg) * raw_pa + reg_ppg * avg_ppg
            init_elo = (1.0 - reg_elo) * base_elo + reg_elo * 1500.0
            self.teams[name] = NBATeamState(
                name=name,
                elo=round(init_elo, 1),
                ppg_for=round(prior_pf, 2),
                ppg_against=round(prior_pa, 2),
            )

    def get_team(self, name: str) -> NBATeamState:
        if name in self.teams:
            return self.teams[name]
        # Fuzzy match by nickname or city
        low = name.lower()
        for k, v in self.teams.items():
            if k.lower() in low or low in k.lower() or k.split()[-1].lower() == low.split()[-1].lower():
                return v
        st = NBATeamState(name=name, elo=1500.0)
        self.teams[name] = st
        return st

    def update_with_results(self, completed_games: list[dict], as_of: str | None = None) -> dict[str, str]:
        """Update ratings game-by-game sequentially using 538 MOV Elo and capped-margin PPG.

        Only uses games completed before `as_of` (or all games if as_of is None).
        Returns a mapping of team -> kickoff_iso of their most recent game (for rest-day calculation).
        """
        K = self.p["elo_k"]
        HCA = self.p["elo_hca"]
        k_prior = self.p.get("prior_games", 14.0)
        mov_cap = self.p.get("mov_cap", 18.0)

        team_games: dict[str, list[tuple[float, float, bool, str, float | None]]] = {t: [] for t in self.teams}
        last_game: dict[str, str] = {}

        for g in completed_games:
            if as_of and g["kickoff"] >= as_of:
                continue
            h, a = g["home"], g["away"]
            try:
                hs, as_ = int(g["home_score"]), int(g["away_score"])
            except (TypeError, ValueError):
                continue

            H = self.get_team(h)
            A = self.get_team(a)

            raw_margin = hs - as_
            diff = H.elo + HCA - A.elo
            exp_h = elo_expect(diff)
            win_h = 1.0 if hs > as_ else 0.0

            # FiveThirtyEight Margin of Victory multiplier
            mov = abs(raw_margin)
            mov_mult = ((mov + 3.0) ** 0.8) / (7.5 + 0.006 * abs(diff))
            shift = K * mov_mult * (win_h - exp_h)
            H.elo = round(H.elo + shift, 1)
            A.elo = round(A.elo - shift, 1)

            # Capped margin for PPG to prevent single blowouts from skewing ratings
            capped_margin = math.copysign(min(abs(raw_margin), mov_cap), raw_margin)
            center = 0.5 * (hs + as_)
            eff_hs = center + 0.5 * capped_margin
            eff_as = center - 0.5 * capped_margin

            g_pace = g.get("pace")
            team_games.setdefault(h, []).append((eff_hs, eff_as, True, g["kickoff"], g_pace))
            team_games.setdefault(a, []).append((eff_as, eff_hs, False, g["kickoff"], g_pace))
            last_game[h] = g["kickoff"]
            last_game[a] = g["kickoff"]

        # Recompute team seasonal stats from games:
        avg_ppg = self.p["league_avg_ppg"]
        reg_ppg = self.p.get("ppg_season_regress", 0.35)
        for name, t in self.teams.items():
            raw_pf, raw_pa = NBA_PRIOR_PPG.get(name, (avg_ppg, avg_ppg))
            prior_pf = (1.0 - reg_ppg) * raw_pf + reg_ppg * avg_ppg
            prior_pa = (1.0 - reg_ppg) * raw_pa + reg_ppg * avg_ppg
            gl = team_games.get(name, [])
            n = len(gl)
            t.wins = sum(1 for x in gl if x[0] > x[1])
            t.losses = n - t.wins
            if n > 0:
                mean_pf = sum(x[0] for x in gl) / n
                mean_pa = sum(x[1] for x in gl) / n
                t.ppg_for = round((n * mean_pf + k_prior * prior_pf) / (n + k_prior), 2)
                t.ppg_against = round((n * mean_pa + k_prior * prior_pa) / (n + k_prior), 2)

                # Possession-adjusted Pace & Offensive/Defensive Rating (per 100 possessions)
                pace_vals = [x[4] for x in gl if x[4] is not None and x[4] > 50.0]
                if pace_vals:
                    mean_pace = sum(pace_vals) / len(pace_vals)
                    t.pace = round((len(pace_vals) * mean_pace + k_prior * 99.5) / (len(pace_vals) + k_prior), 2)
                else:
                    t.pace = 99.5
                t.ortg = round(100.0 * t.ppg_for / max(80.0, t.pace), 2)
                t.drtg = round(100.0 * t.ppg_against / max(80.0, t.pace), 2)

                hw = sum(1 for x in gl if x[2] and x[0] > x[1])
                hl = sum(1 for x in gl if x[2] and x[0] < x[1])
                rw = sum(1 for x in gl if not x[2] and x[0] > x[1])
                rl = sum(1 for x in gl if not x[2] and x[0] < x[1])
                t.home_record = f"{hw}-{hl}"
                t.road_record = f"{rw}-{rl}"

                l10 = gl[-10:]
                l10w = sum(1 for x in l10 if x[0] > x[1])
                t.last10_record = f"{l10w}-{len(l10) - l10w}"

                streak = 0
                for x in reversed(gl):
                    w = x[0] > x[1]
                    if streak == 0:
                        streak = 1 if w else -1
                    elif (streak > 0) == w:
                        streak += 1 if w else -1
                    else:
                        break
                t.streak = streak
            else:
                t.ppg_for = round(prior_pf, 2)
                t.ppg_against = round(prior_pa, 2)
                t.home_record = "0-0"
                t.road_record = "0-0"
                t.last10_record = "0-0"
                t.streak = 0

        return last_game

    def ingest_standings(self, standings_rows: list[dict]):
        """Update team ratings from live ESPN NBA standings, smoothly blended with regressed prior."""
        k_prior = self.p["prior_games"]
        reg_elo = self.p.get("elo_season_regress", 0.35)
        reg_ppg = self.p.get("ppg_season_regress", 0.35)
        ramp_games = self.p.get("elo_ramp_games", 4.5)
        avg_ppg = self.p["league_avg_ppg"]
        for r in standings_rows:
            t = self.get_team(r["team"])
            t.wins = int(r.get("wins", 0))
            t.losses = int(r.get("losses", 0))
            played = t.wins + t.losses
            raw_pf, raw_pa = NBA_PRIOR_PPG.get(t.name, (avg_ppg, avg_ppg))
            prior_pf = (1.0 - reg_ppg) * raw_pf + reg_ppg * avg_ppg
            prior_pa = (1.0 - reg_ppg) * raw_pa + reg_ppg * avg_ppg
            base = (1.0 - reg_elo) * NBA_BASE_ELO.get(t.name, 1500.0) + reg_elo * 1500.0
            if played > 0:
                pf = float(r.get("ppg_for") or prior_pf)
                pa = float(r.get("ppg_against") or prior_pa)
                t.ppg_for = round((played * pf + k_prior * prior_pf) / (played + k_prior), 2)
                t.ppg_against = round((played * pa + k_prior * prior_pa) / (played + k_prior), 2)
                net_diff = pf - pa
                win_pct = t.wins / played
                perf_elo = 1500.0 + net_diff * 22.0 + (win_pct - 0.5) * 180.0
                w_season = played / (played + ramp_games)
                t.elo = (1.0 - w_season) * base + w_season * perf_elo
            else:
                t.ppg_for = round(prior_pf, 2)
                t.ppg_against = round(prior_pa, 2)
                t.elo = base
            t.ortg = round(100.0 * t.ppg_for / max(80.0, t.pace), 2)
            t.drtg = round(100.0 * t.ppg_against / max(80.0, t.pace), 2)
            t.home_record = r.get("home_record", "0-0")
            t.road_record = r.get("road_record", "0-0")
            t.last10_record = r.get("last10", "0-0")
            t.streak = int(r.get("streak", 0))

    def predict(
        self,
        home: str,
        away: str,
        market_odds: list[float] | None = None,
        ou_line: float | None = None,
        spread_line: float | None = None,
        live_adj: dict | None = None,
        rest_days: tuple[int | None, int | None] = (None, None),
    ) -> dict:
        P = self.p
        H = self.get_team(home)
        A = self.get_team(away)
        live_adj = live_adj or {}

        # Situational adjustments (in expected point margin terms for Home)
        home_out = float(live_adj.get("home_out", 0.0))
        away_out = float(live_adj.get("away_out", 0.0))
        inj_pts_h = -min(P["injury_pts_cap"], home_out * P["injury_pts_per_star"])
        inj_pts_a = -min(P["injury_pts_cap"], away_out * P["injury_pts_per_star"])
        net_inj_margin = inj_pts_h - inj_pts_a

        # Rest / Back-to-Back adjustment
        rh, ra = rest_days
        b2b_margin = 0.0
        if rh is not None and ra is not None:
            if rh <= 1 and ra >= 2:
                b2b_margin -= P["b2b_penalty_pts"]
            elif ra <= 1 and rh >= 2:
                b2b_margin += P["b2b_penalty_pts"]

        total_situational_pts = net_inj_margin + b2b_margin
        situational_elo = total_situational_pts * 28.0

        comps = {}

        # 1. Market component (with partial late-scratch injury adjustment if one side is heavily depleted)
        if market_odds and len(market_odds) == 2:
            fair = devig(market_odds)
            if fair:
                p_mkt_raw = min(max(fair[0], 0.02), 0.98)
                mkt_inj_f = P.get("market_inj_factor", 0.55)
                mkt_logit = math.log(p_mkt_raw / (1.0 - p_mkt_raw))
                p_mkt = 1.0 / (1.0 + math.exp(-(mkt_logit + mkt_inj_f * (net_inj_margin / P["margin_sigma"]))))
                comps["market"] = {"p": min(max(p_mkt, 0.02), 0.98), "odds": [round(float(x), 2) for x in market_odds]}

        # 2. Elo component (with HCA + situational Elo)
        elo_diff = (H.elo + P["elo_hca"] - A.elo) + situational_elo
        p_elo = min(max(elo_expect(elo_diff), 0.02), 0.98)
        comps["elo"] = {"p": p_elo, "diff": round(elo_diff)}

        # 3. Possession-Adjusted Net Rating & Expected Score component
        h_off = H.ppg_for
        h_def = H.ppg_against
        a_off = A.ppg_for
        a_def = A.ppg_against

        exp_pace = 0.5 * (H.pace + A.pace)
        hca_pts = P["elo_hca"] / 22.0  # ~2.3 pts home court advantage
        proj_home = (exp_pace / 100.0) * 0.5 * (H.ortg + A.drtg) + 0.5 * hca_pts + 0.5 * total_situational_pts
        proj_away = (exp_pace / 100.0) * 0.5 * (A.ortg + H.drtg) - 0.5 * hca_pts - 0.5 * total_situational_pts
        proj_margin = proj_home - proj_away
        proj_total = proj_home + proj_away

        p_net = min(max(_norm_cdf(proj_margin / P["margin_sigma"]), 0.02), 0.98)
        comps["net_rating"] = {
            "p": p_net,
            "proj_margin": round(proj_margin, 1),
            "proj_total": round(proj_total, 1),
        }

        # 4. Pythagorean Win% (Morey exponent 13.91 + Log5, adjusted for game-day absences)
        exp = P["pyth_exp"]
        ho_adj = max(85.0, h_off + inj_pts_h)
        ao_adj = max(85.0, a_off + inj_pts_a)
        pyth_h = (ho_adj ** exp) / ((ho_adj ** exp) + (h_def ** exp))
        pyth_a = (ao_adj ** exp) / ((ao_adj ** exp) + (a_def ** exp))
        # Log5 with modern home court boost (~0.54 baseline home win rate)
        num = pyth_h * (1.0 - pyth_a) * 0.54
        den = num + (1.0 - pyth_h) * pyth_a * 0.46
        p_pyth = min(max(num / (den or 1.0), 0.02), 0.98)
        comps["pythagorean"] = {"p": p_pyth, "pyth_win_pct": [round(pyth_h, 3), round(pyth_a, 3)]}

        # 5. Form (Last 10 games + active streak momentum)
        l10_hw, l10_hl = _parse_record(H.last10_record)
        l10_aw, l10_al = _parse_record(A.last10_record)
        fh = (l10_hw + 1.5) / (l10_hw + l10_hl + 3.0) + 0.015 * max(-5, min(5, H.streak))
        fa = (l10_aw + 1.5) / (l10_aw + l10_al + 3.0) + 0.015 * max(-5, min(5, A.streak))
        p_form = 1.0 / (1.0 + math.exp(-3.8 * (fh - fa)))
        comps["form"] = {"p": min(max(p_form, 0.05), 0.95), "last10": [H.last10_record, A.last10_record]}

        # 6. Home / Road court split
        hh_w, hh_l = _parse_record(H.home_record)
        ar_w, ar_l = _parse_record(A.road_record)
        hr_h = (hh_w + 1.2) / (hh_w + hh_l + 2.4)
        hr_a = (ar_w + 1.0) / (ar_w + ar_l + 2.2)
        p_hr = 1.0 / (1.0 + math.exp(-4.0 * (hr_h - hr_a)))
        comps["home_road"] = {"p": min(max(p_hr, 0.05), 0.95), "splits": [H.home_record, A.road_record]}

        target_ou = float(ou_line) if ou_line and ou_line > 150 else 226.5
        over_prob = min(max(_norm_cdf((proj_total - target_ou) / 13.5), 0.05), 0.95)

        return {
            "components": comps,
            "proj_home": round(proj_home, 1),
            "proj_away": round(proj_away, 1),
            "proj_total": round(proj_total, 1),
            "proj_margin": round(proj_margin, 1),
            "ou_line": round(target_ou, 1),
            "over_prob": round(over_prob, 4),
            "spread_line": spread_line,
            "features": {
                "elo": [round(H.elo), round(A.elo)],
                "off_ppg": [round(h_off, 1), round(a_off, 1)],
                "def_ppg": [round(h_def, 1), round(a_def, 1)],
                "pace": [round(H.pace, 1), round(A.pace, 1)],
                "ortg": [round(H.ortg, 1), round(A.ortg, 1)],
                "drtg": [round(H.drtg, 1), round(A.drtg, 1)],
                "net_rtg": [round(H.ortg - H.drtg, 1), round(A.ortg - A.drtg, 1)],
                "home_road_splits": [H.home_record, A.road_record],
                "last10": [H.last10_record, A.last10_record],
                "rest_days": [rh, ra],
            },
        }


def blend(components: dict, weights: dict) -> tuple[float, dict]:
    """Combine NBA component probabilities P(Home Wins)."""
    avail = {k: weights.get(k, 0.0) for k in components if weights.get(k, 0.0) > 0}
    tot = sum(avail.values())
    if tot <= 0:
        return 0.5, {}
    norm = {k: v / tot for k, v in avail.items()}
    p_home = sum(norm[k] * components[k]["p"] for k in norm)
    return min(max(p_home, 0.01), 0.99), norm
