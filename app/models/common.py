"""Shared maths: odds conversion, Poisson/Dixon-Coles, Elo maps, scoring metrics."""
from __future__ import annotations

import math

import numpy as np

MAX_GOALS = 10


def devig(odds) -> list[float] | None:
    """Decimal odds -> fair probabilities (proportional margin removal)."""
    try:
        inv = [1.0 / float(o) for o in odds]
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if any(not math.isfinite(x) or x <= 0 for x in inv):
        return None
    s = sum(inv)
    return [x / s for x in inv]


def american_to_decimal(ml) -> float | None:
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        return None
    if ml == 0:
        return None
    return 1 + (ml / 100.0 if ml > 0 else 100.0 / -ml)


_FACT = [math.factorial(i) for i in range(MAX_GOALS + 1)]


def _pois(lam: float) -> np.ndarray:
    return np.array([math.exp(-lam) * lam ** k / _FACT[k] for k in range(MAX_GOALS + 1)])


def score_matrix(lh: float, la: float, rho: float = -0.08) -> np.ndarray:
    """Dixon-Coles adjusted joint score distribution."""
    lh, la = max(lh, 0.05), max(la, 0.05)
    m = np.outer(_pois(lh), _pois(la))
    tau = {(0, 0): 1 - lh * la * rho, (0, 1): 1 + lh * rho, (1, 0): 1 + la * rho, (1, 1): 1 - rho}
    for (i, j), t in tau.items():
        m[i, j] *= max(t, 0.0)
    return m / m.sum()


def matrix_markets(m: np.ndarray) -> dict:
    ph = float(np.tril(m, -1).sum())
    pd_ = float(np.trace(m))
    pa = float(np.triu(m, 1).sum())
    idx = np.add.outer(np.arange(m.shape[0]), np.arange(m.shape[1]))
    over25 = float(m[idx >= 3].sum())
    btts = float(m[1:, 1:].sum())
    i, j = np.unravel_index(np.argmax(m), m.shape)
    return {"1x2": [ph, pd_, pa], "over25": over25, "btts": btts, "top_score": f"{i}-{j}"}


def elo_expect(diff: float) -> float:
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def elo_to_1x2(diff: float, base_draw: float = 0.26) -> list[float]:
    """Map an Elo difference (incl. home advantage) to H/D/A probabilities.
    Draw probability peaks for evenly matched teams and shrinks with the gap."""
    e = elo_expect(diff)
    pdraw = max(0.08, base_draw * (1.0 - 0.9 * (2 * e - 1) ** 2))
    ph = max(0.01, e - pdraw / 2)
    pa = max(0.01, 1 - e - pdraw / 2)
    s = ph + pdraw + pa
    return [ph / s, pdraw / s, pa / s]


def bo5_from_bo3(p3: float) -> float:
    """Convert a best-of-3 match win prob to best-of-5 via the implied set win prob."""
    p3 = min(max(p3, 1e-4), 1 - 1e-4)
    lo, hi = 0.0, 1.0
    for _ in range(50):   # invert P(bo3) = s^2 (3 - 2s)
        s = (lo + hi) / 2
        if s * s * (3 - 2 * s) < p3:
            lo = s
        else:
            hi = s
    s = (lo + hi) / 2
    return s ** 3 * (1 + 3 * (1 - s) + 6 * (1 - s) ** 2)


# ---------------- metrics ----------------
def log_loss(p: np.ndarray, y: np.ndarray) -> float:
    eps = 1e-12
    return float(-np.mean(np.log(np.clip(p[np.arange(len(y)), y], eps, 1))))


def brier(p: np.ndarray, y: np.ndarray) -> float:
    onehot = np.zeros_like(p)
    onehot[np.arange(len(y)), y] = 1
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def accuracy(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean(np.argmax(p, axis=1) == y))


def kelly(p: float, odds: float, fraction: float, cap: float) -> float:
    b = odds - 1
    if b <= 0:
        return 0.0
    f = (p * b - (1 - p)) / b
    return float(min(max(f * fraction, 0.0), cap))
