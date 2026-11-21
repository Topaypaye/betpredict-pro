"""Fast in-memory & disk-cached out-of-sample simulation for the Variables & Logic tab."""
from __future__ import annotations

import threading
from typing import Any

import numpy as np
import pandas as pd

from .config import (
    BASE_DIR,
    FOOTBALL_MARKET_SHARE,
    FOOTBALL_STATS_WEIGHTS,
    NBA_MARKET_SHARE,
    NBA_STATS_WEIGHTS,
    RECO,
    TENNIS_MARKET_SHARE,
    TENNIS_STATS_WEIGHTS,
)
from .models.common import accuracy, brier, log_loss

SIM_DIR = BASE_DIR / "data"
_sim_lock = threading.RLock()
_sim_cache: dict[str, dict[str, Any]] = {}


def _build_football_sim() -> dict[str, Any]:
    npz_path = SIM_DIR / "sim_football.npz"
    if npz_path.exists():
        d = np.load(npz_path, allow_pickle=False)
        return {
            "P": d["P"],
            "y": d["y"],
            "odds_avg": d["odds_avg"],
            "names": list(d["names"]),
            "label": "Out-of-Sample (2025-27 Seasons)",
        }

    from .backtest import FOOTBALL_TEST, collect_football
    from .sources.footballdata import load_all

    df, _ = load_all()
    rows, names = collect_football(df)
    R = pd.DataFrame(rows)
    P_all = np.array(R["P"].tolist(), dtype=float)
    y_all = R["y"].to_numpy(dtype=int)
    oa_all = np.array(R["odds_avg"].tolist(), dtype=float)
    ok = ~np.isnan(P_all).any(axis=(1, 2))
    te = R["season"].isin(FOOTBALL_TEST).to_numpy() & ok
    P, y, oa = P_all[te], y_all[te], oa_all[te]

    SIM_DIR.mkdir(exist_ok=True, parents=True)
    np.savez_compressed(npz_path, P=P, y=y, odds_avg=oa, names=np.array(names))
    return {"P": P, "y": y, "odds_avg": oa, "names": list(names), "label": "Out-of-Sample (2025-27 Seasons)"}


def _build_tennis_sim() -> dict[str, Any]:
    npz_path = SIM_DIR / "sim_tennis.npz"
    if npz_path.exists():
        d = np.load(npz_path, allow_pickle=False)
        return {
            "P": d["P"],
            "y": d["y"],
            "odds_avg": d["odds_avg"],
            "names": list(d["names"]),
            "label": "Out-of-Sample (2026 Tour Season)",
        }

    from .models.tennis import COMPONENTS, TennisEngine
    from .sources.tennisdata import load_all as load_tennis

    df = load_tennis()
    if df.empty:
        return {"P": np.zeros((0, len(COMPONENTS), 2)), "y": np.zeros(0, dtype=int),
                "odds_avg": np.zeros((0, 2)), "names": list(COMPONENTS), "label": "No Tennis Data"}

    years = sorted(int(y) for y in df["year"].unique())
    test_year = years[-1]
    rows = []

    def cb(row, pred, a_is_winner):
        if int(row.year) != test_year:
            return
        c = pred["components"]
        rows.append({
            "y": 0 if a_is_winner else 1,
            "P": [[c[k]["p"], 1.0 - c[k]["p"]] if k in c else [np.nan, np.nan] for k in COMPONENTS],
            "odds_avg": pred["odds_avg"],
        })

    TennisEngine().run(df, cb)
    if not rows:
        return {"P": np.zeros((0, len(COMPONENTS), 2)), "y": np.zeros(0, dtype=int),
                "odds_avg": np.zeros((0, 2)), "names": list(COMPONENTS), "label": f"Tour {test_year}"}

    P_all = np.array([r["P"] for r in rows], dtype=float)
    y_all = np.array([r["y"] for r in rows], dtype=int)
    oa_all = np.array([r["odds_avg"] for r in rows], dtype=float)
    ok = ~np.isnan(P_all).any(axis=(1, 2))
    P, y, oa = P_all[ok], y_all[ok], oa_all[ok]

    SIM_DIR.mkdir(exist_ok=True, parents=True)
    np.savez_compressed(npz_path, P=P, y=y, odds_avg=oa, names=np.array(COMPONENTS))
    return {"P": P, "y": y, "odds_avg": oa, "names": list(COMPONENTS), "label": f"Out-of-Sample ({test_year} Tour Season)"}


def _build_nba_sim() -> dict[str, Any]:
    from .models.nba import COMPONENTS as NBA_COMPONENTS
    from .service import nba

    games = [g for g in nba.all_season_games() if g.get("state") == "post" and g.get("home_score") is not None]
    P_list, y_list, oa_list = [], [], []
    for g in games:
        comps = g.get("components") or {}
        if not all(k in comps for k in ("elo", "net_rating", "pythagorean", "form", "home_road")):
            continue
        hs, as_ = int(g["home_score"]), int(g["away_score"])
        if hs == as_:
            continue
        y_val = 0 if hs > as_ else 1
        row_p = []
        for k in NBA_COMPONENTS:
            if k in comps and comps[k].get("p") is not None:
                p0 = float(comps[k]["p"])
                row_p.append([p0, 1.0 - p0])
            else:
                row_p.append([np.nan, np.nan])
        mkt = g.get("market_odds")
        oa = [float(mkt[0]), float(mkt[1])] if (mkt and len(mkt) >= 2 and mkt[0] and mkt[1]) else [np.nan, np.nan]
        P_list.append(row_p)
        y_list.append(y_val)
        oa_list.append(oa)

    if not P_list:
        return {"P": np.zeros((0, len(NBA_COMPONENTS), 2)), "y": np.zeros(0, dtype=int),
                "odds_avg": np.zeros((0, 2)), "names": list(NBA_COMPONENTS), "label": "2026-27 NBA Season"}

    return {
        "P": np.array(P_list, dtype=float),
        "y": np.array(y_list, dtype=int),
        "odds_avg": np.array(oa_list, dtype=float),
        "names": list(NBA_COMPONENTS),
        "label": "2026-27 NBA Season (Walk-Forward)",
    }


def _get_sim_data(sport: str) -> dict[str, Any]:
    with _sim_lock:
        if sport in _sim_cache:
            return _sim_cache[sport]
        if sport == "football":
            data = _build_football_sim()
        elif sport == "tennis":
            data = _build_tennis_sim()
        elif sport == "nba":
            data = _build_nba_sim()
        else:
            raise ValueError(f"Unknown sport: {sport}")
        _sim_cache[sport] = data
        return data


def _blend_probs(P: np.ndarray, names: list[str], stats_w: dict[str, float], market_share: float) -> np.ndarray:
    """Blend component probabilities with per-match handling for optional market odds."""
    n = len(P)
    if n == 0:
        return np.zeros((0, P.shape[2]))

    stat_names = [nm for nm in names if nm != "market"]
    w_stat = np.array([max(0.0, float(stats_w.get(nm, 0.0))) for nm in stat_names], dtype=float)
    if w_stat.sum() <= 0:
        w_stat = np.ones(len(stat_names), dtype=float)
    w_stat /= w_stat.sum()

    stat_idx = [names.index(nm) for nm in stat_names]
    p_stat = np.einsum("nko,k->no", P[:, stat_idx, :], w_stat)

    if P.shape[2] == 3:
        from .models.football import STAT_DEBIAS
        p_stat = p_stat * STAT_DEBIAS[None, :]
        p_stat /= p_stat.sum(axis=1, keepdims=True)

    ms = float(np.clip(market_share, 0.0, 1.0))
    if "market" not in names or ms == 0.0:
        return p_stat

    mi = names.index("market")
    p_mkt = P[:, mi, :]
    has_mkt = ~np.isnan(p_mkt).any(axis=1)

    p = p_stat.copy()
    if ms == 1.0:
        p[has_mkt] = p_mkt[has_mkt]
        return p

    p[has_mkt] = ms * p_mkt[has_mkt] + (1.0 - ms) * p_stat[has_mkt]

    if P.shape[2] == 3:
        flip = has_mkt & (p.argmax(axis=1) != p_mkt.argmax(axis=1))
        for idx in np.where(flip)[0]:
            st_top = int(p_stat[idx].argmax())
            mk_top = int(p_mkt[idx].argmax())
            if p_stat[idx, st_top] - p_stat[idx, mk_top] < 0.16:
                p[idx] = 0.96 * p_mkt[idx] + 0.04 * p_stat[idx]
    return p


def _eval_metrics(
    p: np.ndarray,
    y: np.ndarray,
    odds_avg: np.ndarray,
    sport: str,
    value_edge_min: float,
) -> dict[str, Any]:
    n = len(y)
    if n == 0:
        return {
            "matches": 0, "hits": 0, "accuracy": 0.0, "log_loss": 0.0, "brier": 0.0,
            "high_conf_threshold": 0.60 if sport == "football" else 0.65,
            "high_conf_win_rate": 0.0, "high_conf_hits": 0, "high_conf_matches": 0,
            "tiers": [], "value_bets": {"bets": 0, "hits": 0, "hit_rate": 0.0, "profit_units": 0.0, "roi": 0.0},
        }

    top_prob = p.max(axis=1)
    top_pick = p.argmax(axis=1)
    correct = top_pick == y

    hc_thr = 0.60 if sport == "football" else 0.65
    hc_mask = top_prob >= hc_thr
    hc_matches = int(hc_mask.sum())
    hc_hits = int(correct[hc_mask].sum()) if hc_matches else 0

    tier_thresholds = (0.0, 0.45, 0.55, 0.60, 0.65, 0.70) if sport == "football" else (0.0, 0.55, 0.60, 0.65, 0.70, 0.75)
    tiers = []
    for thr in tier_thresholds:
        m = top_prob >= thr
        cnt = int(m.sum())
        hits_t = int(correct[m].sum()) if cnt else 0
        tiers.append({
            "label": "All Picks" if thr == 0.0 else f"≥{int(round(thr * 100))}% Conf",
            "threshold": thr,
            "matches": cnt,
            "hits": hits_t,
            "win_rate": round(hits_t / cnt, 4) if cnt else None,
        })

    # Value bets simulation
    idx = np.arange(n)
    edge = np.where(np.isfinite(p * odds_avg), p * odds_avg - 1.0, -9.0)
    edge = np.where(p >= RECO["value_prob_min"], edge, -9.0)
    best_idx = edge.argmax(axis=1)
    best_edge = edge[idx, best_idx]
    best_odds = odds_avg[idx, best_idx]
    vb_mask = (best_edge >= value_edge_min) & np.isfinite(best_odds) & (best_odds > 1.0)
    vb_bets = int(vb_mask.sum())
    if vb_bets > 0:
        vb_won = (best_idx == y) & vb_mask
        vb_hits = int(vb_won.sum())
        vb_profit = float(np.where(vb_won, best_odds - 1.0, -1.0)[vb_mask].sum())
        vb_res = {
            "bets": vb_bets,
            "hits": vb_hits,
            "hit_rate": round(vb_hits / vb_bets, 4),
            "profit_units": round(vb_profit, 2),
            "roi": round(vb_profit / vb_bets, 4),
            "avg_odds": round(float(best_odds[vb_mask].mean()), 2),
        }
    else:
        vb_res = {"bets": 0, "hits": 0, "hit_rate": 0.0, "profit_units": 0.0, "roi": 0.0, "avg_odds": 0.0}

    return {
        "matches": n,
        "hits": int(correct.sum()),
        "accuracy": round(accuracy(p, y), 4),
        "log_loss": round(log_loss(p, y), 4),
        "brier": round(brier(p, y), 4),
        "high_conf_threshold": hc_thr,
        "high_conf_win_rate": round(hc_hits / hc_matches, 4) if hc_matches else 0.0,
        "high_conf_hits": hc_hits,
        "high_conf_matches": hc_matches,
        "tiers": tiers,
        "value_bets": vb_res,
    }


def _default_params(sport: str) -> tuple[float, dict[str, float]]:
    if sport == "football":
        return FOOTBALL_MARKET_SHARE, dict(FOOTBALL_STATS_WEIGHTS)
    if sport == "tennis":
        return TENNIS_MARKET_SHARE, dict(TENNIS_STATS_WEIGHTS)
    if sport == "nba":
        return NBA_MARKET_SHARE, dict(NBA_STATS_WEIGHTS)
    raise ValueError(f"Unknown sport: {sport}")


def simulate_sport(
    sport: str,
    market_share: float | None = None,
    stats_weights: dict[str, float] | None = None,
    value_edge_min: float | None = None,
) -> dict[str, Any]:
    from .service import load_settings, weights as get_saved_weights

    sim = _get_sim_data(sport)
    P, y, oa, names = sim["P"], sim["y"], sim["odds_avg"], sim["names"]

    settings = load_settings()
    if value_edge_min is None:
        value_edge_min = float(settings.get("value_edge_min", 0.04))

    def_ms, def_sw = _default_params(sport)
    saved_w = get_saved_weights(sport)
    saved_ms = float(saved_w.get("market", def_ms))
    saved_sw = {k: v for k, v in saved_w.items() if k != "market"}
    tot_saved = sum(saved_sw.values()) or 1.0
    saved_sw = {k: v / tot_saved for k, v in saved_sw.items()}

    cur_ms = saved_ms if market_share is None else float(market_share)
    cur_sw = saved_sw if stats_weights is None else {k: float(v) for k, v in stats_weights.items()}

    p_cur = _blend_probs(P, names, cur_sw, cur_ms)
    p_def = _blend_probs(P, names, def_sw, def_ms)
    p_sav = _blend_probs(P, names, saved_sw, saved_ms)

    m_cur = _eval_metrics(p_cur, y, oa, sport, value_edge_min)
    m_def = _eval_metrics(p_def, y, oa, sport, value_edge_min)
    m_sav = _eval_metrics(p_sav, y, oa, sport, value_edge_min)

    # Individual variable standalone leaderboard
    components = []
    for i, nm in enumerate(names):
        p_comp = P[:, i, :]
        valid = ~np.isnan(p_comp).any(axis=1)
        cnt = int(valid.sum())
        if cnt == 0:
            continue
        acc = accuracy(p_comp[valid], y[valid])
        ll = log_loss(p_comp[valid], y[valid])
        components.append({
            "name": nm,
            "matches": cnt,
            "accuracy": round(acc, 4),
            "log_loss": round(ll, 4),
        })
    components.sort(key=lambda x: (-x["accuracy"], x["log_loss"]))

    return {
        "sport": sport,
        "sample_label": sim["label"],
        "current": m_cur,
        "default_baseline": m_def,
        "saved_baseline": m_sav,
        "default_params": {"market_share": def_ms, "stats_weights": def_sw},
        "saved_params": {"market_share": saved_ms, "stats_weights": saved_sw},
        "components": components,
    }


def optimize_sport_winrate(sport: str, value_edge_min: float | None = None) -> dict[str, Any]:
    """Search for market_share + stats_weights maximizing out-of-sample prediction winrate."""
    sim = _get_sim_data(sport)
    P, y, oa, names = sim["P"], sim["y"], sim["odds_avg"], sim["names"]
    if len(y) == 0:
        return simulate_sport(sport)

    stat_names = [nm for nm in names if nm != "market"]
    k = len(stat_names)
    _, def_sw = _default_params(sport)

    # Candidate stats weight vectors: default, uniform, single-feature, and reproducible Dirichlet samples
    rng = np.random.default_rng(42)
    cand_sw: list[dict[str, float]] = [
        def_sw,
        {nm: round(1.0 / k, 4) for nm in stat_names},
    ]
    for i in range(k):
        cand_sw.append({nm: (1.0 if j == i else 0.0) for j, nm in enumerate(stat_names)})
    for _ in range(120):
        vec = rng.dirichlet(np.ones(k) * 1.2)
        cand_sw.append({nm: round(float(v), 4) for nm, v in zip(stat_names, vec)})

    best_score = (-1.0, 999.0)
    best_ms = 0.5
    best_sw = def_sw

    # Coarse pass across market_share to find top stats_weights, then fine pass
    for sw in cand_sw:
        for ms in (0.0, 0.2, 0.35, 0.5, 0.65, 0.8, 1.0):
            p = _blend_probs(P, names, sw, ms)
            acc = accuracy(p, y)
            ll = log_loss(p, y)
            score = (round(acc, 5), -ll)
            if score > best_score:
                best_score = score
                best_ms = ms
                best_sw = sw

    # Fine-tune market_share at 0.05 increments around best_sw
    for ms_i in range(0, 21):
        ms = round(ms_i * 0.05, 2)
        p = _blend_probs(P, names, best_sw, ms)
        acc = accuracy(p, y)
        ll = log_loss(p, y)
        score = (round(acc, 5), -ll)
        if score > best_score:
            best_score = score
            best_ms = ms

    res = simulate_sport(sport, market_share=best_ms, stats_weights=best_sw, value_edge_min=value_edge_min)
    res["optimized_params"] = {"market_share": best_ms, "stats_weights": best_sw}
    return res
