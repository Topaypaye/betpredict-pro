"""Walk-forward back-testing and weight tuning.

Both sports follow the same protocol:
  1. ratings are built match-by-match, every prediction uses only prior data;
  2. the STATS weights (no market) are tuned by minimising log-loss on TRAIN;
  3. everything is evaluated OUT-OF-SAMPLE on TEST: each component alone, the
     stats model, the final blend (stats + market) and the market alone, plus
     calibration and flat-stake betting simulations.

Run:  python -m app.backtest            (football, and tennis if files present)
      python -m app.backtest football   /  tennis
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from .config import (BASE_DIR, FOOTBALL_MARKET_SHARE, FOOTBALL_STATS_WEIGHTS, RECO,
                     TENNIS_MARKET_SHARE, TENNIS_STATS_WEIGHTS, WEIGHTS_FILE, effective_weights)
from .models.common import accuracy, brier, log_loss

OUT_DIR = BASE_DIR / "data"
OUT_DIR.mkdir(exist_ok=True, parents=True)
FOOTBALL_TRAIN = ("2223", "2324", "2425")
FOOTBALL_TEST = ("2526", "2627")


# ---------------------------------------------------------------- generic helpers
def tune_weights(P: np.ndarray, y: np.ndarray, names: list[str], iters: int = 3000, lr: float = 0.5) -> dict:
    """Exponentiated-gradient minimisation of log-loss over the weight simplex.
    P has shape (n_rows, n_components, n_outcomes)."""
    k = P.shape[1]
    w = np.full(k, 1.0 / k)
    py = P[np.arange(len(y)), :, y]
    for _ in range(iters):
        mix = py @ w
        grad = -(py / mix[:, None]).mean(axis=0)
        w = w * np.exp(-lr * grad)
        w /= w.sum()
    return {n: round(float(x), 4) for n, x in zip(names, w)}


def blend_matrix(P: np.ndarray, weights: dict, names: list[str]) -> np.ndarray:
    w = np.array([weights.get(n, 0.0) for n in names], dtype=float)
    w = w / w.sum()
    if P.shape[2] == 3 and "market" in names:
        from .models.football import STAT_DEBIAS
        mi = names.index("market")
        ms = w[mi]
        if 0.0 < ms < 1.0:
            w_stat = w.copy()
            w_stat[mi] = 0.0
            w_stat /= w_stat.sum()
            p_stat = np.einsum("nko,k->no", P, w_stat) * STAT_DEBIAS[None, :]
            p_stat /= p_stat.sum(axis=1, keepdims=True)
            p_mkt = P[:, mi, :]
            p = ms * p_mkt + (1.0 - ms) * p_stat
            flip = p.argmax(axis=1) != p_mkt.argmax(axis=1)
            for idx in np.where(flip)[0]:
                st_top = int(p_stat[idx].argmax())
                mk_top = int(p_mkt[idx].argmax())
                if p_stat[idx, st_top] - p_stat[idx, mk_top] < 0.16:
                    p[idx] = 0.96 * p_mkt[idx] + 0.04 * p_stat[idx]
            return p
    return np.einsum("nko,k->no", P, w)


def calibration(p: np.ndarray, y: np.ndarray, bins=(0, .3, .4, .5, .6, .7, .8, .9, 1.01)) -> list:
    """Reliability of the predicted top-pick probability."""
    top, hit = p.max(axis=1), (p.argmax(axis=1) == y)
    out = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (top >= lo) & (top < hi)
        if m.sum():
            out.append({"bin": f"{lo:.0%}-{min(hi, 1):.0%}", "n": int(m.sum()),
                        "predicted": round(float(top[m].mean()), 3), "actual": round(float(hit[m].mean()), 3)})
    return out


def strategies(p: np.ndarray, y: np.ndarray, odds_avg: np.ndarray, odds_max: np.ndarray) -> dict:
    """Flat 1-unit staking results for simple rules."""
    n = len(y)
    idx = np.arange(n)

    def summary(mask, pick, odds):
        o = odds[idx, pick]
        mask = mask & np.isfinite(o)
        bets = int(mask.sum())
        if not bets:
            return {"bets": 0}
        won = (pick == y) & mask
        profit = float(np.where(won, o - 1, -1)[mask].sum())
        return {"bets": bets, "hit_rate": round(float(won.sum() / bets), 3), "profit_units": round(profit, 1),
                "roi": round(profit / bets, 3), "avg_odds": round(float(o[mask].mean()), 2)}

    res = {}
    top = p.argmax(axis=1)
    res["top_pick_all@avg"] = summary(np.ones(n, bool), top, odds_avg)
    res["top_pick_conf>=60%@avg"] = summary(p.max(axis=1) >= RECO["conf_high"], top, odds_avg)
    res["top_pick_conf>=70%@avg"] = summary(p.max(axis=1) >= 0.70, top, odds_avg)
    for name, odds in (("avg", odds_avg), ("max", odds_max)):
        edge = np.where(np.isfinite(p * odds), p * odds - 1, -9)
        edge = np.where(p >= RECO["value_prob_min"], edge, -9)
        best = edge.argmax(axis=1)
        for thr in (0.02, 0.04, 0.08):
            res[f"value_edge>={int(thr * 100)}%@{name}"] = summary(edge[idx, best] >= thr, best, odds)
    return res


def metrics(p, y):
    return {"log_loss": round(log_loss(p, y), 4), "brier": round(brier(p, y), 4), "accuracy": round(accuracy(p, y), 4)}


def evaluate(R, P_all, y_all, mask, names, stats_w, market_share, label, group_cols=()):
    P, y = P_all[mask], y_all[mask]
    oa = np.array(R.loc[mask, "odds_avg"].tolist(), dtype=float)
    om = np.array(R.loc[mask, "odds_max"].tolist(), dtype=float)
    k = P.shape[2]
    base = np.tile([np.mean(y == i) for i in range(k)], (len(y), 1))
    out = {"label": label, "matches": int(mask.sum()),
           "components": {"base_rates": metrics(base, y), **{n: metrics(P[:, i], y) for i, n in enumerate(names)}},
           "blends": {}}
    final_w = effective_weights(stats_w, market_share)
    stats_only = {**stats_w, "market": 0.0}
    for bname, w in (("final", final_w), ("stats_model", stats_only), ("market_only", {"market": 1.0})):
        pb = blend_matrix(P, w, names)
        out["blends"][bname] = {"weights": {kk: round(v, 4) for kk, v in w.items()}, **metrics(pb, y),
                                "calibration": calibration(pb, y), "betting": strategies(pb, y, oa, om)}
    ps = blend_matrix(P, stats_only, names)
    out["market_share_curve"] = [
        {"market_share": ms, **metrics(ms * P[:, names.index("market")] + (1 - ms) * ps, y)}
        for ms in (0.0, 0.2, 0.4, 0.5, 0.6, 0.65, 0.7, 0.8, 0.9, 1.0)]
    pf = blend_matrix(P, final_w, names)
    for col in group_cols:
        g = R.loc[mask, col].to_numpy()
        out[f"per_{col}"] = {str(v): {"n": int((g == v).sum()), **metrics(pf[g == v], y[g == v])} for v in sorted(set(g))}
    return out


# ---------------------------------------------------------------- football
def collect_football(df: pd.DataFrame):
    from .models.football import COMPONENTS, FootballEngine
    eng = FootballEngine()
    rows = []

    def cb(row, pred):
        c = pred["components"]
        rows.append({
            "date": row.date, "league": row.league, "season": row.season, "home": row.home, "away": row.away,
            "score": f"{int(row.hg)}-{int(row.ag)}",
            "y": 0 if row.hg > row.ag else (1 if row.hg == row.ag else 2),
            "over25": int(row.hg + row.ag > 2),
            "P": [c[k]["p"] if k in c else [np.nan] * 3 for k in COMPONENTS],
            "ou_model": 0.5 * (c["xg_poisson"]["over25"] + c["goals_poisson"]["over25"]),
            "odds_avg": [row.oh_avg, row.od_avg, row.oa_avg],
            "odds_max": [row.oh_max, row.od_max, row.oa_max],
            "xg_real": bool(row.xg_real),
        })
    eng.run(df, cb)
    return rows, COMPONENTS


def backtest_football(train=FOOTBALL_TRAIN, test=FOOTBALL_TEST) -> dict:
    from .sources.footballdata import load_all
    df, meta = load_all()
    rows, names = collect_football(df)
    R = pd.DataFrame(rows)
    P_all = np.array(R["P"].tolist(), dtype=float)
    y_all = R["y"].to_numpy()
    ok = ~np.isnan(P_all).any(axis=(1, 2))
    tr = R["season"].isin(train).to_numpy() & ok
    te = R["season"].isin(test).to_numpy() & ok
    stats_names = [n for n in names if n != "market"]
    tuned = tune_weights(P_all[tr][:, [names.index(n) for n in stats_names]], y_all[tr], stats_names)
    result = {
        "sport": "football", "data": meta, "train_seasons": list(train), "test_seasons": list(test),
        "tuned_stats_weights": tuned, "market_share": FOOTBALL_MARKET_SHARE,
        "default_stats_weights": FOOTBALL_STATS_WEIGHTS,
        "train": evaluate(R, P_all, y_all, tr, names, tuned, FOOTBALL_MARKET_SHARE, "train (weights fitted here)"),
        "test": evaluate(R, P_all, y_all, te, names, tuned, FOOTBALL_MARKET_SHARE, "test (out-of-sample)",
                         group_cols=("league", "season")),
    }
    # over/under 2.5 (stats model only)
    ou, o25 = R.loc[te, "ou_model"].to_numpy(), R.loc[te, "over25"].to_numpy()
    result["test"]["over_under_2_5"] = {"accuracy": round(float(np.mean((ou > 0.5) == (o25 == 1))), 3),
                                        "actual_over_rate": round(float(o25.mean()), 3),
                                        "log_loss": round(log_loss(np.column_stack([ou, 1 - ou]), 1 - o25), 4)}
    # decisive / no-draw outcomes
    decisive_mask = (y_all[te] != 1)
    pf_te = blend_matrix(P_all[te], effective_weights(tuned, FOOTBALL_MARKET_SHARE), names)
    top_te = pf_te.argmax(axis=1)
    n_dec = int(decisive_mask.sum())
    hits_dec = int((top_te[decisive_mask] == y_all[te][decisive_mask]).sum())
    result["test"]["no_draw"] = {
        "matches": n_dec,
        "draws": int(len(y_all[te]) - n_dec),
        "draw_rate": round(float((y_all[te] == 1).mean()), 3),
        "accuracy": round(hits_dec / n_dec, 3) if n_dec else 0,
        "hits": hits_dec,
    }
    # recent examples
    pf = blend_matrix(P_all, effective_weights(tuned, FOOTBALL_MARKET_SHARE), names)
    lab = ["H", "D", "A"]
    ex = []
    for i in np.where(te)[0][::-1][:60]:
        r = R.iloc[i]
        ex.append({"date": str(r["date"].date()), "league": r["league"], "match": f'{r["home"]} - {r["away"]}',
                   "score": r["score"], "probs": [round(float(x), 3) for x in pf[i]], "pick": lab[int(pf[i].argmax())],
                   "result": lab[int(r["y"])], "hit": bool(pf[i].argmax() == r["y"]),
                   "odds_avg": [None if pd.isna(x) else float(x) for x in r["odds_avg"]]})
    result["recent_predictions"] = ex
    return result


# ---------------------------------------------------------------- tennis
def backtest_tennis() -> dict | None:
    from .models.tennis import COMPONENTS, TennisEngine
    from .sources.tennisdata import load_all as load_tennis
    df = load_tennis()
    if df.empty or df["year"].nunique() < 2:
        return None
    years = sorted(int(y) for y in df["year"].unique())
    test_year = years[-1]
    train_years = years[1:-1] if len(years) > 2 else years[:-1]   # first year = warm-up
    rows = []

    def cb(row, pred, a_is_winner):
        c = pred["components"]
        ws = int(row.wsets) if not pd.isna(row.wsets) else (2 if a_is_winner else 0)
        ls = int(row.lsets) if not pd.isna(row.lsets) else (0 if a_is_winner else 2)
        score_str = f"{ws}-{ls}" if a_is_winner else f"{ls}-{ws}"
        rows.append({"year": int(row.year), "tour": row.tour, "date": str(row.date)[:10], "surface": row.surface,
                     "match": f'{pred["a"]} vs {pred["b"]}', "a": pred["a"], "b": pred["b"],
                     "score": score_str, "y": 0 if a_is_winner else 1,
                     "P": [[c[k]["p"], 1 - c[k]["p"]] if k in c else [np.nan, np.nan] for k in COMPONENTS],
                     "odds_avg": pred["odds_avg"], "odds_max": pred["odds_max"]})
    TennisEngine().run(df, cb)
    R = pd.DataFrame(rows)
    P_all = np.array(R["P"].tolist(), dtype=float)
    y_all = R["y"].to_numpy()
    ok = ~np.isnan(P_all).any(axis=(1, 2))
    tr = R["year"].isin(train_years).to_numpy() & ok
    te = (R["year"] == test_year).to_numpy() & ok
    stats_names = [n for n in COMPONENTS if n != "market"]
    tuned = tune_weights(P_all[tr][:, [COMPONENTS.index(n) for n in stats_names]], y_all[tr], stats_names)

    # recent examples (out-of-sample)
    eff_w = effective_weights(tuned, TENNIS_MARKET_SHARE)
    pf = blend_matrix(P_all, eff_w, COMPONENTS)
    ex = []
    for i in np.where(te)[0][::-1][:60]:
        r = R.iloc[i]
        pick_name = r["a"] if pf[i][0] >= pf[i][1] else r["b"]
        result_name = r["a"] if r["y"] == 0 else r["b"]
        is_hit = bool((pf[i][0] >= pf[i][1]) == (r["y"] == 0))
        ex.append({
            "date": str(r["date"])[:10],
            "tour": r["tour"],
            "surface": r["surface"],
            "match": r["match"],
            "score": r["score"],
            "probs": [round(float(pf[i][0]), 3), round(float(pf[i][1]), 3)],
            "pick": pick_name,
            "result": result_name,
            "hit": is_hit,
            "odds_avg": [None if pd.isna(x) else float(x) for x in r["odds_avg"]] if r["odds_avg"] is not None else None,
        })

    return {"sport": "tennis", "years": years, "train_years": train_years, "test_year": test_year,
            "tuned_stats_weights": tuned, "market_share": TENNIS_MARKET_SHARE,
            "default_stats_weights": TENNIS_STATS_WEIGHTS,
            "train": evaluate(R, P_all, y_all, tr, COMPONENTS, tuned, TENNIS_MARKET_SHARE, "train"),
            "test": evaluate(R, P_all, y_all, te, COMPONENTS, tuned, TENNIS_MARKET_SHARE,
                             f"test {test_year} (out-of-sample)", group_cols=("surface", "tour")),
            "recent_predictions": ex}


# ---------------------------------------------------------------- CLI
def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if np.isnan(o) else float(o)
    return o


def _print(res):
    t = res["test"]
    print(f"[{res['sport']}] {t['label']}  matches={t['matches']}")
    for k, v in t["components"].items():
        print(f"   {k:14s} acc={v['accuracy']:.3f}  logloss={v['log_loss']:.4f}")
    for k, v in t["blends"].items():
        print(f"   BLEND {k:12s} acc={v['accuracy']:.3f}  logloss={v['log_loss']:.4f}")
    print("   tuned stats weights:", res["tuned_stats_weights"])
    for k, v in t["blends"]["final"]["betting"].items():
        print(f"     final  {k:26s} {v}")
    for k, v in t["blends"]["stats_model"]["betting"].items():
        print(f"     stats  {k:26s} {v}")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    saved = json.loads(WEIGHTS_FILE.read_text()) if WEIGHTS_FILE.exists() else {}
    if "tennis" not in argv:
        fb = backtest_football()
        (OUT_DIR / "backtest_football.json").write_text(json.dumps(_jsonable(fb), indent=1, default=str))
        saved["football"] = {"stats": fb["tuned_stats_weights"]}
        _print(fb)
    if "football" not in argv:
        tn = backtest_tennis()
        if tn:
            (OUT_DIR / "backtest_tennis.json").write_text(json.dumps(_jsonable(tn), indent=1, default=str))
            saved["tennis"] = {"stats": tn["tuned_stats_weights"]}
            _print(tn)
        else:
            print("[tennis] no tennis-data files in data/tennis - skipped")
    WEIGHTS_FILE.write_text(json.dumps(saved, indent=1))


if __name__ == "__main__":
    main()
