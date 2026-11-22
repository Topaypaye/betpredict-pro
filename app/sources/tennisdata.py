"""tennis-data.co.uk loader.

The site's terms ask that files are downloaded by private individuals and not by
bots, so the app does NOT download them. Save the yearly Excel files manually
into  data/tennis/  (any sub-folder, e.g. data/tennis/atp/2026.xlsx and
data/tennis/wta/2026.xlsx). ATP vs WTA is detected from the file's columns.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import TENNIS_DIR


def _num(df, *cols):
    for c in cols:
        if c in df.columns:
            return pd.to_numeric(df[c], errors="coerce")
    return pd.Series(np.nan, index=df.index)


def _load_file(path) -> pd.DataFrame:
    try:
        raw = pd.read_excel(path)
    except Exception as e:  # noqa: BLE001
        print(f"[tennisdata] cannot read {path}: {e}")
        return pd.DataFrame()
    if "Winner" not in raw.columns:
        return pd.DataFrame()
    tour = "WTA" if "WTA" in raw.columns or "Tier" in raw.columns else "ATP"
    df = pd.DataFrame({
        "tour": tour,
        "date": pd.to_datetime(raw["Date"], errors="coerce"),
        "tournament": raw.get("Tournament", ""),
        "location": raw.get("Location", ""),
        "series": raw.get("Series", raw.get("Tier", "")),
        "court": raw.get("Court", ""),
        "surface": raw.get("Surface", "Hard").fillna("Hard"),
        "round": raw.get("Round", ""),
        "best_of": _num(raw, "Best of").fillna(3).astype(int),
        "winner": raw["Winner"].astype(str).str.strip(),
        "loser": raw["Loser"].astype(str).str.strip(),
        "wrank": _num(raw, "WRank"), "lrank": _num(raw, "LRank"),
        "wpts": _num(raw, "WPts"), "lpts": _num(raw, "LPts"),
        "wsets": _num(raw, "Wsets"), "lsets": _num(raw, "Lsets"),
        "comment": raw.get("Comment", "Completed").fillna("Completed").astype(str),
        "avgw": _num(raw, "AvgW"), "avgl": _num(raw, "AvgL"),
        "maxw": _num(raw, "MaxW"), "maxl": _num(raw, "MaxL"),
        "b365w": _num(raw, "B365W"), "b365l": _num(raw, "B365L"),
        "psw": _num(raw, "PSW"), "psl": _num(raw, "PSL"),
    })
    df = df.dropna(subset=["date"])
    df["year"] = df["date"].dt.year
    return df


def load_all() -> pd.DataFrame:
    cache_csv = TENNIS_DIR / "tennis_all.csv.gz"
    files = sorted(list(TENNIS_DIR.rglob("*.xlsx")) + list(TENNIS_DIR.rglob("*.xls")))
    if cache_csv.exists():
        newest_xlsx = max((p.stat().st_mtime for p in files), default=0.0)
        if cache_csv.stat().st_mtime >= newest_xlsx:
            try:
                df = pd.read_csv(cache_csv, parse_dates=["date"])
                return df
            except Exception:
                pass
    frames = [f for f in (_load_file(p) for p in files) if not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["tour", "date", "winner", "loser"])
    df = df.sort_values(["date", "tour"]).reset_index(drop=True)
    try:
        df.to_csv(cache_csv, index=False, compression="gzip")
    except Exception:
        pass
    return df


def files_status() -> list[dict]:
    out = []
    for p in sorted(list(TENNIS_DIR.rglob("*.xlsx")) + list(TENNIS_DIR.rglob("*.xls"))):
        out.append({"file": str(p.relative_to(TENNIS_DIR)), "size_kb": round(p.stat().st_size / 1024)})
    return out
