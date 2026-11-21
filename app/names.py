"""Team / player name matching across data sources (ESPN, football-data.co.uk, APIs)."""
from __future__ import annotations

import difflib
import re
import unicodedata

# canonical football-data.co.uk names <- common alternative spellings
ALIASES = {
    # England
    "Man United": ["manchester united", "man utd", "manchester utd"],
    "Man City": ["manchester city"],
    "Nott'm Forest": ["nottingham forest", "nottm forest", "notts forest"],
    "Wolves": ["wolverhampton wanderers", "wolverhampton"],
    "Newcastle": ["newcastle united"],
    "Tottenham": ["tottenham hotspur", "spurs"],
    "West Ham": ["west ham united"],
    "Brighton": ["brighton & hove albion", "brighton and hove albion"],
    "Leeds": ["leeds united"],
    "Leicester": ["leicester city"],
    "Ipswich": ["ipswich town"],
    "Bournemouth": ["afc bournemouth"],
    "Sheffield United": ["sheffield utd"],
    "Luton": ["luton town"],
    "Norwich": ["norwich city"],
    "Sunderland": ["sunderland afc"],
    # Spain
    "Ath Madrid": ["atletico madrid", "atletico de madrid", "atlético madrid", "club atletico de madrid"],
    "Ath Bilbao": ["athletic club", "athletic bilbao"],
    "Betis": ["real betis"],
    "Sociedad": ["real sociedad"],
    "Celta": ["celta vigo", "rc celta"],
    "Vallecano": ["rayo vallecano"],
    "Espanol": ["espanyol", "rcd espanyol"],
    "Alaves": ["deportivo alaves", "alavés"],
    "Leganes": ["leganés"],
    "Cadiz": ["cádiz"],
    "Almeria": ["almería"],
    "La Coruna": ["deportivo la coruna", "deportivo de la coruna", "deportivo"],
    "Oviedo": ["real oviedo"],
    "Valladolid": ["real valladolid"],
    "Mallorca": ["rcd mallorca"],
    # Italy
    "Inter": ["internazionale", "inter milan"],
    "Milan": ["ac milan"],
    "Verona": ["hellas verona"],
    "Roma": ["as roma"],
    "Napoli": ["ssc napoli"],
    # Germany
    "Bayern Munich": ["bayern munchen", "fc bayern munich", "bayern"],
    "Dortmund": ["borussia dortmund"],
    "M'gladbach": ["borussia monchengladbach", "monchengladbach", "gladbach"],
    "Ein Frankfurt": ["eintracht frankfurt"],
    "Leverkusen": ["bayer leverkusen", "bayer 04 leverkusen"],
    "FC Koln": ["fc cologne", "1. fc koln", "cologne", "koln"],
    "Mainz": ["mainz 05", "1. fsv mainz 05", "fsv mainz"],
    "Hoffenheim": ["tsg hoffenheim", "1899 hoffenheim"],
    "Stuttgart": ["vfb stuttgart"],
    "Hamburg": ["hamburg sv", "hamburger sv"],
    "St Pauli": ["fc st. pauli", "st. pauli"],
    "Union Berlin": ["1. fc union berlin"],
    "Werder Bremen": ["sv werder bremen", "bremen"],
    "Wolfsburg": ["vfl wolfsburg"],
    "Freiburg": ["sc freiburg"],
    "Augsburg": ["fc augsburg"],
    "Heidenheim": ["1. fc heidenheim", "1. fc heidenheim 1846"],
    "Bochum": ["vfl bochum"],
    "Darmstadt": ["sv darmstadt 98"],
    "Holstein Kiel": ["kiel"],
    "Hertha": ["hertha berlin", "hertha bsc"],
    "RB Leipzig": ["leipzig"],
    "Elversberg": ["sv elversberg"],
    "Paderborn": ["sc paderborn 07", "sc paderborn"],
    # France
    "Paris SG": ["paris saint-germain", "paris saint germain", "psg"],
    "St Etienne": ["saint-etienne", "as saint-etienne", "saint etienne"],
    "Marseille": ["olympique marseille", "olympique de marseille"],
    "Lyon": ["olympique lyonnais"],
    "Lille": ["losc lille"],
    "Monaco": ["as monaco"],
    "Rennes": ["stade rennais"],
    "Brest": ["stade brestois", "stade brestois 29"],
    "Reims": ["stade de reims"],
    "Nice": ["ogc nice"],
    "Lens": ["rc lens"],
    "Strasbourg": ["rc strasbourg", "rc strasbourg alsace"],
    "Paris FC": ["paris fc"],
    "Le Havre": ["le havre ac"],
    "Auxerre": ["aj auxerre"],
    "Angers": ["angers sco"],
    "Nantes": ["fc nantes"],
    "Toulouse": ["toulouse fc"],
    "Lorient": ["fc lorient"],
    "Metz": ["fc metz"],
}

_STOP = {"fc", "cf", "afc", "sc", "ac", "as", "ssc", "rc", "cd", "ud", "sd", "rcd", "club", "de", "the", "calcio", "1."}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return " ".join(t for t in s.split() if t not in _STOP)


_ALIAS_LOOKUP = {}
for canon, alts in ALIASES.items():
    for a in [canon, *alts]:
        _ALIAS_LOOKUP[norm(a)] = canon


def match_team(name: str, candidates: list[str]) -> str | None:
    """Map an external team name to one of the football-data `candidates`."""
    if not candidates:
        return None
    if name in candidates:
        return name
    n = norm(name)
    canon = _ALIAS_LOOKUP.get(n)
    if canon in candidates:
        return canon
    if canon:
        for c in candidates:
            if _ALIAS_LOOKUP.get(norm(c)) == canon:
                return c
    by_norm = {norm(c): c for c in candidates}
    if n in by_norm:
        return by_norm[n]
    for nc, c in by_norm.items():          # token containment ("Brentford FC" ~ "Brentford")
        c_canon = _ALIAS_LOOKUP.get(nc)
        if canon and c_canon and c_canon != canon:
            continue
        if nc and (nc in n.split() or n in nc.split() or nc == n.replace(" ", "")):
            return c
    best = difflib.get_close_matches(n, list(by_norm), n=1, cutoff=0.72)
    return by_norm[best[0]] if best else None


def tennis_key(full_name: str) -> str:
    """'Jannik Sinner' -> 'Sinner J.' (tennis-data.co.uk style)."""
    parts = unicodedata.normalize("NFKD", full_name).encode("ascii", "ignore").decode().split()
    if len(parts) < 2:
        return full_name
    return f"{' '.join(parts[1:])} {parts[0][0]}."


def match_player(full_name: str, known: list[str]) -> str | None:
    """Match an ESPN full name to tennis-data style 'Lastname F.' names."""
    if not known:
        return None
    parts = unicodedata.normalize("NFKD", full_name).encode("ascii", "ignore").decode().replace("-", " ").split()
    if len(parts) < 2:
        return None
    first_initial = parts[0][0].lower()
    kn = {k: k.lower().replace("-", " ") for k in known}
    # try every split point (handles 'Van De Zandschulp', 'Auger Aliassime', 'Davidovich Fokina')
    for i in range(1, len(parts)):
        last = " ".join(parts[i:]).lower()
        for k, kl in kn.items():
            if kl.startswith(last + " ") and kl.split()[-1].startswith(first_initial):
                return k
    last = parts[-1].lower()
    cands = [k for k, kl in kn.items() if kl.split()[0] == last and kl.split()[-1].startswith(first_initial)]
    return cands[0] if len(cands) == 1 else None
