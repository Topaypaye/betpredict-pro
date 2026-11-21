"""Tiny disk cache for HTTP downloads (keeps us polite with free data sources)."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import requests

from .config import CACHE_DIR

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BetApp/1.0 (personal use)"}
_session = requests.Session()
_session.headers.update(UA)


def _path(url: str, ext: str) -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()[:20]
    return CACHE_DIR / f"{h}.{ext}"


def fetch_bytes(url: str, ttl: int, ext: str = "bin", timeout: int = 25) -> bytes | None:
    """Return cached bytes if younger than ttl, else download. Falls back to stale cache on error."""
    p = _path(url, ext)
    if p.exists() and time.time() - p.stat().st_mtime < ttl:
        return p.read_bytes()
    try:
        r = _session.get(url, timeout=timeout)
        if r.status_code == 200 and r.content:
            p.write_bytes(r.content)
            return r.content
    except requests.RequestException:
        pass
    return p.read_bytes() if p.exists() else None


def fetch_json(url: str, ttl: int, timeout: int = 25):
    b = fetch_bytes(url, ttl, "json", timeout)
    if b is None:
        return None
    try:
        return json.loads(b)
    except ValueError:
        return None
