"""Tennis prediction engine (online, leak-free; same code for live + back-test).

Components (each returns P(player A wins)):
  market       de-vigged bookmaker odds
  surface_elo  50/50 blend of overall Elo and surface-specific Elo
  elo          overall Elo
  ranking      logistic on log(rank ratio)
  form         recent win rate (last N days), shrunk
  h2h          head-to-head, shrunk to 50%
Adjustments: fatigue (sets played in the previous 7 days), best-of-5 scaling.
"""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field

import pandas as pd

from ..config import TENNIS_PARAMS
from .common import bo5_from_bo3, devig, elo_expect

COMPONENTS = ["market", "surface_elo", "elo", "ranking", "form", "h2h"]
SURFACES = ("Hard", "Clay", "Grass", "Carpet")


@dataclass
class Player:
    elo: float = 1500.0
    n: int = 0
    s_elo: dict = field(default_factory=lambda: defaultdict(lambda: 1500.0))
    s_n: dict = field(default_factory=lambda: defaultdict(int))
    rank: float | None = None
    results: deque = field(default_factory=lambda: deque(maxlen=60))   # (date, won, sets_played)


class TennisEngine:
    def __init__(self, params: dict | None = None):
        self.p = {**TENNIS_PARAMS, **(params or {})}
        self.players: dict[tuple, Player] = defaultdict(Player)
        self.h2h: dict[tuple, list] = defaultdict(list)

    def _k(self, n):
        P = self.p
        return P["elo_k_base"] / (n + P["elo_k_offset"]) ** P["elo_k_shape"]

    def _form(self, pl: Player, date):
        cutoff = date - pd.Timedelta(days=self.p["form_days"])
        rec = [r for r in pl.results if r[0] >= cutoff and r[0] < date]
        w = sum(r[1] for r in rec)
        k = self.p["form_prior"]
        return (w + 0.5 * k) / (len(rec) + k), len(rec), w

    def _sets_last7(self, pl: Player, date):
        cutoff = date - pd.Timedelta(days=7)
        return sum(r[2] for r in pl.results if cutoff <= r[0] < date)

    def predict(self, tour, a, b, surface, best_of, date, odds=None, rank_a=None, rank_b=None) -> dict:
        date = pd.Timestamp(date)
        surface = surface if surface in SURFACES else "Hard"
        A, B = self.players[(tour, a)], self.players[(tour, b)]
        P = self.p
        comps = {}
        bo5 = int(best_of or 3) == 5
        scale = (lambda x: bo5_from_bo3(x)) if bo5 else (lambda x: x)

        fat_a, fat_b = self._sets_last7(A, date), self._sets_last7(B, date)
        fat = max(-P["fatigue_cap"], min(P["fatigue_cap"], -P["fatigue_sets_penalty"] * (fat_a - fat_b)))

        if odds is not None:
            fair = devig(odds)
            if fair:
                comps["market"] = {"p": fair[0], "odds": [float(o) for o in odds]}
        pe = elo_expect(A.elo - B.elo)
        comps["elo"] = {"p": min(max(scale(pe) + fat, 0.01), 0.99), "diff": round(A.elo - B.elo)}
        sa = 0.5 * A.elo + 0.5 * A.s_elo[surface]
        sb = 0.5 * B.elo + 0.5 * B.s_elo[surface]
        comps["surface_elo"] = {"p": min(max(scale(elo_expect(sa - sb)) + fat, 0.01), 0.99), "diff": round(sa - sb)}
        ra = rank_a if rank_a and not pd.isna(rank_a) else (A.rank or 300)
        rb = rank_b if rank_b and not pd.isna(rank_b) else (B.rank or 300)
        comps["ranking"] = {"p": scale(1 / (1 + math.exp(-P["rank_coef"] * (math.log(rb) - math.log(ra))))),
                            "ranks": [int(ra), int(rb)]}
        fa, na, wa = self._form(A, date)
        fb, nb, wb = self._form(B, date)
        comps["form"] = {"p": scale(1 / (1 + math.exp(-3.0 * (fa - fb)))), "record": [f"{wa}-{na-wa}", f"{wb}-{nb-wb}"]}
        games = self.h2h[tuple(sorted((a, b)))]
        ha = sum(1 for g in games if g[1] == a)
        k = P["h2h_prior"]
        comps["h2h"] = {"p": (ha + 0.5 * k) / (len(games) + k), "record": f"{ha}-{len(games)-ha}"}

        features = {
            "elo": [round(A.elo), round(B.elo)],
            "surface_elo": [round(A.s_elo[surface]), round(B.s_elo[surface])],
            "matches": [A.n, B.n], "surface_matches": [A.s_n[surface], B.s_n[surface]],
            "rank": [int(ra), int(rb)],
            "form_winrate": [round(fa, 3), round(fb, 3)],
            "form_record": [f"{wa}-{na-wa}", f"{wb}-{nb-wb}"],
            "h2h": f"{ha}-{len(games)-ha}",
            "sets_last_7d": [fat_a, fat_b], "fatigue_adj": round(fat, 3),
            "surface": surface, "best_of": 5 if bo5 else 3,
        }
        return {"components": comps, "features": features, "a": a, "b": b}

    def update(self, tour, winner, loser, surface, date, sets_w=0, sets_l=0, wrank=None, lrank=None):
        date = pd.Timestamp(date)
        surface = surface if surface in SURFACES else "Hard"
        W, L = self.players[(tour, winner)], self.players[(tour, loser)]
        e = elo_expect(W.elo - L.elo)
        W.elo += self._k(W.n) * (1 - e)
        L.elo -= self._k(L.n) * (1 - e)
        es = elo_expect(W.s_elo[surface] - L.s_elo[surface])
        kw, kl = self._k(W.s_n[surface]), self._k(L.s_n[surface])
        W.s_elo[surface] += kw * (1 - es)
        L.s_elo[surface] -= kl * (1 - es)
        W.n += 1; L.n += 1; W.s_n[surface] += 1; L.s_n[surface] += 1
        sw = 0 if (pd.isna(sets_w) or sets_w is None) else int(sets_w)
        sl = 0 if (pd.isna(sets_l) or sets_l is None) else int(sets_l)
        sets = sw + sl
        W.results.append((date, 1, sets)); L.results.append((date, 0, sets))
        if wrank and not pd.isna(wrank): W.rank = float(wrank)
        if lrank and not pd.isna(lrank): L.rank = float(lrank)
        self.h2h[tuple(sorted((winner, loser)))].append((date, winner))

    def run(self, df: pd.DataFrame, callback=None, callback_min_date=None):
        min_ts = pd.Timestamp(callback_min_date) if callback_min_date is not None else None
        for r in df.itertuples(index=False):
            c = str(r.comment).lower()
            if "walkover" in c or c.startswith("w/o"):
                continue
            if callback is not None and c.startswith("completed") and (min_ts is None or r.date >= min_ts):
                # deterministic pseudo-random side assignment avoids "A always wins" leakage
                a_is_w = int(hashlib.md5(f"{r.date}{r.winner}{r.loser}".encode()).hexdigest(), 16) % 2 == 0
                a, b = (r.winner, r.loser) if a_is_w else (r.loser, r.winner)
                oa, ob = (r.avgw, r.avgl) if a_is_w else (r.avgl, r.avgw)
                ma, mb = (r.maxw, r.maxl) if a_is_w else (r.maxl, r.maxw)
                ra, rb = (r.wrank, r.lrank) if a_is_w else (r.lrank, r.wrank)
                odds = None if (pd.isna(oa) or pd.isna(ob)) else [oa, ob]
                pred = self.predict(r.tour, a, b, r.surface, r.best_of, r.date, odds, ra, rb)
                pred["odds_avg"] = [oa, ob]
                pred["odds_max"] = [ma, mb]
                callback(r, pred, a_is_w)
            self.update(r.tour, r.winner, r.loser, r.surface, r.date, r.wsets, r.lsets, r.wrank, r.lrank)

    def known_players(self, tour):
        return [k[1] for k in self.players if k[0] == tour]


def blend(components: dict, weights: dict) -> tuple[float, dict]:
    used = {k: weights.get(k, 0.0) for k in components if weights.get(k, 0.0) > 0}
    tot = sum(used.values())
    if tot <= 0:
        return 0.5, {}
    p = sum((w / tot) * components[k]["p"] for k, w in used.items())
    return float(p), {k: w / tot for k, w in used.items()}
