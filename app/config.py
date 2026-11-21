"""Central configuration: leagues, data sources and the model coefficients.

Every coefficient used by the prediction engines lives here so it can be
displayed in the UI, tuned by the back-tester and overridden at runtime.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("CACHE_DIR", BASE_DIR / "data_cache"))
TENNIS_DIR = Path(os.environ.get("TENNIS_DIR", BASE_DIR / "data" / "tennis"))
WEIGHTS_FILE = BASE_DIR / "data" / "weights.json"   # tuned weights (written by backtest)
STATIC_DIR = BASE_DIR / "static"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
TENNIS_DIR.mkdir(parents=True, exist_ok=True)

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")          # optional basic protection
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")          # optional: the-odds-api.com

# --------------------------------------------------------------------------
# Leagues  (football-data.co.uk code, ESPN slug, The-Odds-API key)
# --------------------------------------------------------------------------
LEAGUES = {
    "EPL":        {"name": "Premier League",         "country": "England", "fd": "E0",  "espn": "eng.1",          "odds_api": "soccer_epl"},
    "LALIGA":     {"name": "LaLiga",                 "country": "Spain",   "fd": "SP1", "espn": "esp.1",          "odds_api": "soccer_spain_la_liga"},
    "SERIEA":     {"name": "Serie A",                "country": "Italy",   "fd": "I1",  "espn": "ita.1",          "odds_api": "soccer_italy_serie_a"},
    "BUNDESLIGA": {"name": "Bundesliga",             "country": "Germany", "fd": "D1",  "espn": "ger.1",          "odds_api": "soccer_germany_bundesliga"},
    "LIGUE1":     {"name": "Ligue 1",                "country": "France",  "fd": "F1",  "espn": "fra.1",          "odds_api": "soccer_france_ligue_one"},
    "UCL":        {"name": "UEFA Champions League",  "country": "Europe",  "fd": None,  "espn": "uefa.champions", "odds_api": "soccer_uefa_champs_league"},
    "UEL":        {"name": "UEFA Europa League",     "country": "Europe",  "fd": None,  "espn": "uefa.europa",    "odds_api": "soccer_uefa_europa_league"},
    "NATIONS":    {"name": "UEFA Nations League",    "country": "Europe",  "fd": None,  "espn": "uefa.nations",   "odds_api": "soccer_uefa_nations_league"},
}

# Seasons pulled from football-data.co.uk (oldest first). "2627" = 2026/27.
FOOTBALL_SEASONS = ["2122", "2223", "2324", "2425", "2526", "2627"]
CURRENT_SEASON = "2627"

# --------------------------------------------------------------------------
# FOOTBALL model
# --------------------------------------------------------------------------
# Two-tier blend (linear opinion pool):
#   stats_model = sum(w_i * component_i)   over the independent statistical components
#   final       = MARKET_SHARE * market + (1 - MARKET_SHARE) * stats_model
# Stats weights below come from the walk-forward back-test (log-loss tuned on
# 2022/23-2024/25, validated on 2025/26-2026/27). Values in WEIGHTS_FILE win.
FOOTBALL_STATS_WEIGHTS = {
    "xg_poisson":    0.40,   # Dixon-Coles Poisson on time-weighted, opponent-adjusted xG
    "elo":           0.38,   # Elo rating with home advantage & goal-margin multiplier
    "goals_poisson": 0.14,   # Dixon-Coles Poisson on time-weighted real goals
    "form":          0.03,   # last-5 points per game + xG difference
    "table":         0.03,   # season-to-date PPG + goal difference
    "h2h":           0.02,   # shrunk head-to-head record
}
FOOTBALL_MARKET_SHARE = 0.65  # weight of de-vigged bookmaker consensus in the final number


def effective_weights(stats_w: dict, market_share: float) -> dict:
    """Flatten the two tiers into one weight per component (sums to 1)."""
    tot = sum(stats_w.values()) or 1.0
    out = {k: (1 - market_share) * v / tot for k, v in stats_w.items()}
    out["market"] = market_share
    return out


FOOTBALL_WEIGHTS = effective_weights(FOOTBALL_STATS_WEIGHTS, FOOTBALL_MARKET_SHARE)

# Adjustments applied to expected goals (lambda) of the Poisson components.
FOOTBALL_ADJ = {
    "injury_per_key_player": 0.035,   # -3.5% own xG per unavailable key player (cap below)
    "injury_cap":            0.15,    # max -15%
    "promoted_attack":       0.90,    # promoted side: x0.90 attack prior
    "promoted_defence":      1.12,    # promoted side: x1.12 goals conceded prior
    "rest_short_days":       3,       # <= 3 days of rest is "short"
    "rest_penalty":          0.03,    # -3% own xG when short rest and opponent is not
}

# Hyper-parameters of the individual components (grid-searched on 2022-2025).
FOOTBALL_PARAMS = {
    "elo_k": 22.0,
    "elo_hfa": 46.0,              # home-field advantage in Elo points
    "elo_season_regress": 0.20,   # pull to league mean between seasons
    "elo_promoted_offset": -90.0, # promoted team starts at league mean - 90
    "ewma_alpha": 0.085,          # weight of newest match in xG/goal ratings (~12-match memory)
    "dc_rho": -0.11,              # Dixon-Coles low-score correlation
    "form_n": 5,
    "form_scale_ppg": 110.0,      # Elo points per 1.0 PPG difference (last 5)
    "form_scale_xgd": 90.0,       # Elo points per 1.0 xG-diff/game difference (last 5)
    "table_scale_ppg": 130.0,
    "table_scale_gd": 70.0,
    "table_prior_games": 6,       # shrink early-season table toward league average
    "h2h_n": 6,
    "h2h_years": 5,
    "h2h_prior": 6.0,             # pseudo-matches of league base rates
}

# --------------------------------------------------------------------------
# TENNIS model  (same two-tier logic)
# --------------------------------------------------------------------------
TENNIS_STATS_WEIGHTS = {
    "surface_elo":  0.45,   # 50/50 overall + surface Elo
    "elo":          0.20,   # overall Elo
    "ranking":      0.15,   # official ranking (log ratio)
    "form":         0.12,   # recent win rate (120 days)
    "h2h":          0.08,   # head-to-head, shrunk
}
TENNIS_MARKET_SHARE = 0.65
TENNIS_WEIGHTS = effective_weights(TENNIS_STATS_WEIGHTS, TENNIS_MARKET_SHARE)
TENNIS_PARAMS = {
    "elo_k_base": 250.0, "elo_k_offset": 5.0, "elo_k_shape": 0.4,   # FiveThirtyEight-style K
    "rank_coef": 0.95,            # logistic coefficient on log(rank ratio)
    "form_days": 120, "form_prior": 4.0,
    "h2h_prior": 3.0,
    "fatigue_sets_penalty": 0.012, # -1.2pp per extra set played in last 7 days vs opponent
    "fatigue_cap": 0.06,
}

# --------------------------------------------------------------------------
# NBA model  (two-tier statistical + bookmaker consensus)
# --------------------------------------------------------------------------
NBA_STATS_WEIGHTS = {
    "elo":          0.32,   # FiveThirtyEight NBA Elo with Margin-of-Victory & +50 HCA
    "net_rating":   0.28,   # Offensive vs Defensive PPG efficiency & point differential
    "pythagorean":  0.14,   # Morey Pythagorean win expectation (exponent 13.91) + Log5
    "form":         0.14,   # Last-10 games momentum & active win/loss streak
    "home_road":    0.12,   # Home court vs Road court split efficiency
}
NBA_MARKET_SHARE = 0.30
NBA_SEASON_START = "2026-10-20"   # first regular-season day (point-in-time ratings are rebuilt from here)
NBA_WEIGHTS = effective_weights(NBA_STATS_WEIGHTS, NBA_MARKET_SHARE)
NBA_PARAMS = {
    "elo_k": 20.0,
    "elo_hca": 50.0,              # Modern NBA home-court advantage in Elo points (~2.2 pts margin)
    "elo_season_regress": 0.45,   # Offseason mean-reversion toward 1500 (standard for high roster/injury turnover)
    "ppg_season_regress": 0.40,   # Offseason PPG prior regression toward league average
    "league_avg_ppg": 114.5,      # NBA baseline points per team per game
    "margin_sigma": 11.5,         # Standard deviation of NBA final margin
    "pyth_exp": 13.91,            # Morey basketball Pythagorean exponent
    "prior_games": 14.0,          # Shrinkage prior games early in season (stabilizes early Net Rating)
    "mov_cap": 18.0,              # Margin-of-victory cap for PPG (prevents blowouts from skewing ratings)
    "elo_ramp_games": 4.5,        # Games for current-season performance Elo to reach 50% weight
    "b2b_penalty_pts": 2.2,       # -2.2 pts margin penalty on back-to-back (0 rest days)
    "injury_pts_per_star": 2.8,   # -2.8 pts margin per missing key starter/star
    "injury_pts_cap": 11.0,       # max -11.0 pts
    "market_inj_factor": 0.55,    # Late-scratch injury adjustment applied to raw bookmaker line
}

# Impact weights (in "star units", where 1.0 = -2.8 pts margin) for NBA stars & key starters
NBA_STAR_WEIGHTS = {
    # Tier 1: Franchise MVP / All-NBA Superstars (1.15 - 1.25 star units)
    "Nikola Jokic": 1.25, "Luka Doncic": 1.20, "Shai Gilgeous-Alexander": 1.20,
    "Giannis Antetokounmpo": 1.20, "Victor Wembanyama": 1.15, "Jayson Tatum": 1.15,
    "Joel Embiid": 1.15, "Anthony Edwards": 1.10, "Stephen Curry": 1.10,
    "Kevin Durant": 1.10, "LeBron James": 1.05, "Anthony Davis": 1.05,
    "Donovan Mitchell": 1.05, "Jalen Brunson": 1.05, "Devin Booker": 1.05,
    # Tier 2: All-Stars & Primary Starters (0.45 - 0.85 star units)
    "Jaylen Brown": 0.85, "Tyrese Haliburton": 0.85, "Paolo Banchero": 0.85,
    "Cade Cunningham": 0.85, "Ja Morant": 0.85, "Trae Young": 0.85,
    "Kawhi Leonard": 0.85, "James Harden": 0.80, "Damian Lillard": 0.80,
    "Kyrie Irving": 0.80, "Domantas Sabonis": 0.80, "De'Aaron Fox": 0.80,
    "Bam Adebayo": 0.80, "Jimmy Butler": 0.80, "Tyrese Maxey": 0.80,
    "Karl-Anthony Towns": 0.80, "Chet Holmgren": 0.80, "Jalen Williams": 0.80,
    "Franz Wagner": 0.75, "Scottie Barnes": 0.75, "Alperen Sengun": 0.75,
    "Zion Williamson": 0.80, "Brandon Ingram": 0.70, "LaMelo Ball": 0.80,
    "Jalen Johnson": 0.75, "Cooper Flagg": 0.75, "Evan Mobley": 0.75,
    "Darius Garland": 0.70, "Pascal Siakam": 0.75, "Jamal Murray": 0.75,
    "Aaron Gordon": 0.65, "Lauri Markkanen": 0.75, "Desmond Bane": 0.70,
    "Jaren Jackson Jr.": 0.75, "Julius Randle": 0.70, "Rudy Gobert": 0.70,
    "Derrick White": 0.70, "Kristaps Porzingis": 0.70, "Jrue Holiday": 0.65,
    "Paul George": 0.75, "DeMar DeRozan": 0.70, "Zach LaVine": 0.70,
    "Coby White": 0.65, "Josh Giddey": 0.60, "Mikal Bridges": 0.65,
    "OG Anunoby": 0.65, "Austin Reaves": 0.65, "Amen Thompson": 0.65,
    "Fred VanVleet": 0.65, "Jalen Green": 0.65, "Devin Vassell": 0.65,
    "Stephon Castle": 0.60, "Dyson Daniels": 0.60, "Trey Murphy III": 0.65,
    "CJ McCollum": 0.65, "Bradley Beal": 0.65, "Norman Powell": 0.60,
    "Ivica Zubac": 0.60, "Tyler Herro": 0.70, "Immanuel Quickley": 0.65,
    "RJ Barrett": 0.65, "Brandon Miller": 0.70, "Miles Bridges": 0.65,
    "Anfernee Simons": 0.65, "Deni Avdija": 0.60, "Scoot Henderson": 0.60,
    "Shaedon Sharpe": 0.60, "Keyonte George": 0.60, "Walker Kessler": 0.60,
    "Nic Claxton": 0.60, "Cam Thomas": 0.65, "Michael Porter Jr.": 0.65,
    "Andrew Wiggins": 0.55, "Draymond Green": 0.65, "Jonathan Kuminga": 0.55,
    "Grant Williams": 0.45, "Kon Knueppel": 0.50, "Mitchell Robinson": 0.50,
    "Robert Williams III": 0.55, "Donovan Clingan": 0.55, "Toumani Camara": 0.55,
    "Zach Edey": 0.60, "Scotty Pippen Jr.": 0.50, "Cameron Boozer": 0.75,
    # High-impact starters, defenders & 6th men
    "Jalen Suggs": 0.65, "Jonathan Isaac": 0.50, "Kentavious Caldwell-Pope": 0.50, "Cole Anthony": 0.45,
    "Keegan Murray": 0.65, "Malik Monk": 0.60, "Harrison Barnes": 0.50, "Alex Caruso": 0.60,
    "Cason Wallace": 0.50, "Luguentz Dort": 0.60, "Isaiah Hartenstein": 0.65, "Jalen Duren": 0.60,
    "Jaden Ivey": 0.65, "Ausar Thompson": 0.55, "Tobias Harris": 0.60, "Malik Beasley": 0.50,
    "Bobby Portis": 0.55, "Brook Lopez": 0.65, "Kelly Oubre Jr.": 0.55, "Buddy Hield": 0.55,
    "Brandin Podziemski": 0.55, "De'Anthony Melton": 0.50, "Kevon Looney": 0.45, "Christian Braun": 0.55,
    "Russell Westbrook": 0.55, "Peyton Watson": 0.45, "Tari Eason": 0.55, "Reed Sheppard": 0.45,
    "Steven Adams": 0.50, "Caris LeVert": 0.55, "Max Strus": 0.55, "Isaac Okoro": 0.50,
    "Donte DiVincenzo": 0.60, "Naz Reid": 0.65, "Jaden McDaniels": 0.60, "Mike Conley": 0.55,
    "Nickeil Alexander-Walker": 0.50, "Herbert Jones": 0.65, "Jordan Hawkins": 0.45, "Yves Missi": 0.45,
    "Daniel Gafford": 0.60, "P.J. Washington": 0.60, "Dereck Lively II": 0.65, "Klay Thompson": 0.60,
    "Naji Marshall": 0.50, "Collin Sexton": 0.60, "John Collins": 0.60, "Bennedict Mathurin": 0.65,
    "Myles Turner": 0.70, "Andrew Nembhard": 0.60, "Aaron Nesmith": 0.55, "T.J. McConnell": 0.50,
    "Vince Williams Jr.": 0.50, "GG Jackson": 0.50, "Marcus Smart": 0.60, "Santi Aldama": 0.55,
    "Bogdan Bogdanovic": 0.60, "De'Andre Hunter": 0.60, "Clint Capela": 0.60, "Onyeka Okongwu": 0.55,
    "Terry Rozier": 0.60, "Duncan Robinson": 0.50, "Jaime Jaquez Jr.": 0.55, "Tre Jones": 0.50,
    "Keldon Johnson": 0.60, "Zach Collins": 0.50, "Alex Sarr": 0.55, "Bilal Coulibaly": 0.55,
    "Jordan Poole": 0.65, "Jonas Valanciunas": 0.60,
}

# --------------------------------------------------------------------------
# Recommendation rules
# --------------------------------------------------------------------------
RECO = {
    "value_edge_min": 0.04,     # model_prob * odds - 1 >= 4% to flag a value bet
    "value_prob_min": 0.25,     # ignore long shots below 25% model probability
    "kelly_fraction": 0.25,     # quarter-Kelly stake suggestion
    "kelly_cap": 0.05,          # never more than 5% of bankroll
    "conf_high": 0.60,
    "conf_medium": 0.45,
}

CACHE_TTL = {
    "fd_current": 6 * 3600,     # current-season CSV
    "fd_past": 30 * 86400,      # completed seasons
    "fd_fixtures": 3 * 3600,
    "espn_scoreboard": 15 * 60,
    "espn_team": 6 * 3600,
    "espn_news": 30 * 60,
    "odds_api": 60 * 60,
}
