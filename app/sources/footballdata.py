"""football-data.co.uk loader: results, shots, xG (from 2026/27) and bookmaker odds."""
from __future__ import annotations

import io

import numpy as np
import pandas as pd

from ..cache import fetch_bytes
from ..config import CACHE_TTL, CURRENT_SEASON, FOOTBALL_SEASONS, LEAGUES

BASE = "https://www.football-data.co.uk"


def _read_csv(b: bytes) -> pd.DataFrame:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(b), encoding=enc, on_bad_lines="skip")
        except Exception:  # noqa: BLE001
            continue
    return pd.DataFrame()


def _col(df: pd.DataFrame, *names):
    for n in names:
        if n in df.columns:
            return pd.to_numeric(df[n], errors="coerce")
    return pd.Series(np.nan, index=df.index)


def _normalise(df: pd.DataFrame, league: str, season: str | None) -> pd.DataFrame:
    if df.empty or "HomeTeam" not in df.columns:
        return pd.DataFrame()
    df = df.dropna(subset=["HomeTeam", "AwayTeam"]).copy()
    out = pd.DataFrame({
        "league": league,
        "season": season,
        "date": pd.to_datetime(df["Date"], dayfirst=True, errors="coerce"),
        "time": df.get("Time", pd.Series("", index=df.index)).fillna(""),
        "home": df["HomeTeam"].str.strip(),
        "away": df["AwayTeam"].str.strip(),
        "hg": _col(df, "FTHG"), "ag": _col(df, "FTAG"),
        "hthg": _col(df, "HTHG"), "htag": _col(df, "HTAG"),
        "hxg_real": _col(df, "HxG"), "axg_real": _col(df, "AxG"),
        "hs": _col(df, "HS"), "as_": _col(df, "AS"),
        "hst": _col(df, "HST"), "ast": _col(df, "AST"),
        "hc": _col(df, "HC"), "ac": _col(df, "AC"),
        "hr": _col(df, "HR"), "ar": _col(df, "AR"),
        # pre-match odds (collected ~Fri/Tue before the round)
        "oh_avg": _col(df, "AvgH", "BbAvH"), "od_avg": _col(df, "AvgD", "BbAvD"), "oa_avg": _col(df, "AvgA", "BbAvA"),
        "oh_max": _col(df, "MaxH", "BbMxH"), "od_max": _col(df, "MaxD", "BbMxD"), "oa_max": _col(df, "MaxA", "BbMxA"),
        "oh_b365": _col(df, "B365H"), "od_b365": _col(df, "B365D"), "oa_b365": _col(df, "B365A"),
        "oh_ps": _col(df, "PSH"), "od_ps": _col(df, "PSD"), "oa_ps": _col(df, "PSA"),
        # closing odds (sharp line movement & late lineup/injury consensus)
        "oh_cavg": _col(df, "AvgCH"), "od_cavg": _col(df, "AvgCD"), "oa_cavg": _col(df, "AvgCA"),
        "oh_cmax": _col(df, "MaxCH"), "od_cmax": _col(df, "MaxCD"), "oa_cmax": _col(df, "MaxCA"),
        "oh_cps": _col(df, "PSCH"), "od_cps": _col(df, "PSCD"), "oa_cps": _col(df, "PSCA"),
        # over/under 2.5
        "oo25_avg": _col(df, "Avg>2.5", "BbAv>2.5"), "ou25_avg": _col(df, "Avg<2.5", "BbAv<2.5"),
        "oo25_max": _col(df, "Max>2.5", "BbMx>2.5"), "ou25_max": _col(df, "Max<2.5", "BbMx<2.5"),
    })
    return out.dropna(subset=["date"])


def load_season(league: str, season: str) -> pd.DataFrame:
    code = LEAGUES[league]["fd"]
    if not code:
        return pd.DataFrame()
    ttl = CACHE_TTL["fd_current"] if season == CURRENT_SEASON else CACHE_TTL["fd_past"]
    b = fetch_bytes(f"{BASE}/mmz4281/{season}/{code}.csv", ttl, "csv")
    if not b:
        return pd.DataFrame()
    return _normalise(_read_csv(b), league, season)


def fit_xg_proxy(df: pd.DataFrame) -> tuple[float, float, float, float, float]:
    """Least-squares fit xG ~ a*shots + b*shots_on_target + c*corners + d*ht_goals + e."""
    rows = []
    for s, st, c, ht, xg in (("hs", "hst", "hc", "hthg", "hxg_real"), ("as_", "ast", "ac", "htag", "axg_real")):
        sub = df[[s, st, c, ht, xg]].dropna()
        rows.append(sub.rename(columns={s: "s", st: "st", c: "c", ht: "ht", xg: "xg"}))
    d = pd.concat(rows)
    if len(d) < 50:
        return 0.0805, 0.1014, -0.0237, 0.2200, -0.0965   # calibrated 4-feature fallback
    X = np.column_stack([d["s"], d["st"], d["c"], d["ht"], np.ones(len(d))])
    coef, *_ = np.linalg.lstsq(X, d["xg"].to_numpy(), rcond=None)
    return float(coef[0]), float(coef[1]), float(coef[2]), float(coef[3]), float(coef[4])


def load_all(seasons=None) -> tuple[pd.DataFrame, dict]:
    """All leagues & seasons, chronologically sorted, with an `hxg`/`axg` column
    that is real xG when available and a 4-feature (shots, SOT, corners, HT goals) proxy otherwise."""
    seasons = seasons or FOOTBALL_SEASONS
    frames = [load_season(lg, s) for lg in LEAGUES for s in seasons]
    df = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    a, b, c, d, e = fit_xg_proxy(df)
    proxy_h = (a * df["hs"].fillna(12) + b * df["hst"].fillna(4) + c * df["hc"].fillna(5) + d * df["hthg"].fillna(0.6) + e).clip(lower=0.08)
    proxy_a = (a * df["as_"].fillna(10) + b * df["ast"].fillna(3.5) + c * df["ac"].fillna(4) + d * df["htag"].fillna(0.5) + e).clip(lower=0.08)
    df["xg_real"] = df["hxg_real"].notna()
    df["hxg"] = df["hxg_real"].fillna(proxy_h).fillna(df["hg"])
    df["axg"] = df["axg_real"].fillna(proxy_a).fillna(df["ag"])
    df = df.sort_values(["date", "league", "home"]).reset_index(drop=True)
    meta = {"xg_proxy": {"shots": round(a, 4), "sot": round(b, 4), "corners": round(c, 4), "ht_goals": round(d, 4), "const": round(e, 4)},
            "rows": int(len(df)), "rows_real_xg": int(df["xg_real"].sum())}
    return df, meta


def load_fixtures() -> pd.DataFrame:
    """Upcoming fixtures with multi-bookmaker odds (published ahead of each round)."""
    b = fetch_bytes(f"{BASE}/fixtures.csv", CACHE_TTL["fd_fixtures"], "csv")
    if not b:
        return pd.DataFrame()
    raw = _read_csv(b)
    code_to_league = {v["fd"]: k for k, v in LEAGUES.items()}
    if "Div" not in raw.columns:
        return pd.DataFrame()
    raw = raw[raw["Div"].isin(code_to_league)]
    frames = [_normalise(g, code_to_league[div], CURRENT_SEASON) for div, g in raw.groupby("Div")]
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
