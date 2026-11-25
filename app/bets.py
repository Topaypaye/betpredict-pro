"""User Bet Tracker & Financial Bankroll Manager (persisted to data/user_bets.json)."""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from .config import BASE_DIR

BETS_FILE = BASE_DIR / "data" / "user_bets.json"
_bets_lock = threading.RLock()


def _load_raw() -> dict[str, Any]:
    with _bets_lock:
        if BETS_FILE.exists():
            try:
                data = json.loads(BETS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("starting_capital", 1000.0)
                    data.setdefault("currency", "€")
                    data.setdefault("bets", [])
                    return data
            except Exception:
                pass
        return {"starting_capital": 1000.0, "currency": "€", "bets": []}


def _save_raw(data: dict[str, Any]) -> None:
    with _bets_lock:
        data["updated_at"] = time.time()
        BETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        BETS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _build_live_match_lookup(pending_items: list[dict[str, Any]] | None = None, force_fetch: bool = False) -> dict[str, dict[str, Any]]:
    """Index finished/live matches across Football, NBA, and Tennis by espn_id for auto-settlement.

    If any pending bet/leg is missing or not yet final in memory caches, queries ESPN directly
    so bets settle immediately even on cold start or stale cache.
    """
    from . import config as C
    from .service import football, nba, tennis
    from .sources import espn

    by_id: dict[str, dict[str, Any]] = {}
    # 1. Football cached leagues
    for lg, (_, fix_list) in list(football._fixtures_cache.items()):
        for f in fix_list or []:
            eid = str(f.get("espn_id") or "")
            if eid:
                by_id[eid] = {"sport": "football", "item": f}
    # 2. NBA cached games
    for g in list(nba._all_games_cache or []):
        eid = str(g.get("espn_id") or "")
        if eid:
            by_id[eid] = {"sport": "nba", "item": g}
    # 3. Tennis cached matches
    for m in list(tennis._matches_cache or []):
        eid = str(m.get("espn_id") or "")
        if eid:
            by_id[eid] = {"sport": "tennis", "item": m}

    # Determine which pending espn_ids still need a live ESPN check
    needed_sports: set[str] = set()
    needed_fb_leagues: set[str] = set()
    for p in pending_items or []:
        if (p.get("status") or "pending").lower() != "pending" or p.get("manual_status"):
            continue
        legs = p.get("legs") or [p]
        for lg in legs:
            if (lg.get("status") or "pending").lower() != "pending":
                continue
            eid = str(lg.get("espn_id") or "")
            if not eid:
                continue
            cached_entry = by_id.get(eid)
            if force_fetch or not cached_entry or cached_entry["item"].get("state") != "post":
                sp = (lg.get("sport") or p.get("sport") or "").lower()
                if sp:
                    needed_sports.add(sp)
                if sp == "football":
                    lcode = str(lg.get("league") or "").upper()
                    if lcode in C.LEAGUES:
                        needed_fb_leagues.add(lcode)

    if "nba" in needed_sports:
        try:
            for ev in espn.nba_fixtures(days_back=6, days_ahead=1):
                eid = str(ev.get("espn_id") or "")
                if eid:
                    by_id[eid] = {"sport": "nba", "item": ev}
        except Exception:
            pass

    if "football" in needed_sports:
        target_lgs = list(needed_fb_leagues) if needed_fb_leagues else list(C.LEAGUES.keys())
        for lcode in target_lgs:
            slug = (C.LEAGUES.get(lcode) or {}).get("espn")
            if not slug:
                continue
            try:
                for ev in espn.soccer_fixtures(slug):
                    eid = str(ev.get("espn_id") or "")
                    if eid:
                        by_id[eid] = {"sport": "football", "item": ev}
            except Exception:
                pass

    if "tennis" in needed_sports:
        for tour in ("atp", "wta"):
            try:
                for m in espn.tennis_matches(tour):
                    eid = str(m.get("espn_id") or "")
                    if eid:
                        by_id[eid] = {"sport": "tennis", "item": m}
            except Exception:
                pass

    return by_id


def _try_settle_single_leg(leg: dict[str, Any], lookup: dict[str, dict[str, Any]]) -> bool:
    """Auto-settle one single bet or one parlay leg if its match is finished."""
    if leg.get("status") != "pending" or leg.get("manual_status"):
        return False
    eid = str(leg.get("espn_id") or "")
    hit_entry = lookup.get(eid) if eid else None
    if not hit_entry:
        return False

    sp = hit_entry["sport"]
    m = hit_entry["item"]
    if m.get("state") != "post":
        return False

    pcode = (leg.get("pick_code") or "").upper()
    pick_str = (leg.get("pick") or "").strip().lower()

    if sp in ("football", "nba"):
        if m.get("home_score") is None or m.get("away_score") is None:
            return False
        try:
            hs = int(m["home_score"])
            as_ = int(m["away_score"])
        except (TypeError, ValueError):
            return False
        leg["score"] = f"{hs}-{as_}"
        home_name = (m.get("home") or "").strip().lower()
        away_name = (m.get("away") or "").strip().lower()

        won: bool | None = None
        if pcode == "HOME":
            won = hs > as_
        elif pcode == "AWAY":
            won = as_ > hs
        elif pcode == "DRAW":
            won = hs == as_
        elif pcode == "OVER_25":
            won = (hs + as_) > 2.5
        elif pcode == "UNDER_25":
            won = (hs + as_) < 2.5
        elif pcode == "BTTS_YES":
            won = hs > 0 and as_ > 0
        elif pcode == "BTTS_NO":
            won = hs == 0 or as_ == 0
        else:
            if home_name and home_name in pick_str:
                won = hs > as_
            elif away_name and away_name in pick_str:
                won = as_ > hs
            elif "draw" in pick_str:
                won = hs == as_

        if won is not None:
            leg["status"] = "won" if won else "lost"
            return True

    elif sp == "tennis":
        if not (m.get("a_winner") or m.get("b_winner")):
            return False
        leg["score"] = m.get("score") or leg.get("score")
        a_won = bool(m.get("a_winner"))
        a_name = (m.get("a") or "").strip().lower()
        b_name = (m.get("b") or "").strip().lower()
        won = None
        if pcode in ("HOME", "A"):
            won = a_won
        elif pcode in ("AWAY", "B"):
            won = not a_won
        elif a_name and a_name in pick_str:
            won = a_won
        elif b_name and b_name in pick_str:
            won = not a_won
        if won is not None:
            leg["status"] = "won" if won else "lost"
            return True

    return False


def _try_settle_bet(bet: dict[str, Any], lookup: dict[str, dict[str, Any]]) -> bool:
    """Auto-settle a single bet or a multi-bet (parlay) across all its legs."""
    legs = bet.get("legs")
    if not legs:
        return _try_settle_single_leg(bet, lookup)

    changed = False
    for leg in legs:
        if _try_settle_single_leg(leg, lookup):
            changed = True

    if not bet.get("manual_status"):
        leg_statuses = [(l.get("status") or "pending").lower() for l in legs]
        old_st = bet.get("status")
        if any(s == "lost" for s in leg_statuses):
            new_st = "lost"
        elif all(s in ("won", "void") for s in leg_statuses) and any(s == "won" for s in leg_statuses):
            new_st = "won"
        elif all(s == "void" for s in leg_statuses):
            new_st = "void"
        else:
            new_st = "pending"
        if new_st != old_st:
            bet["status"] = new_st
            changed = True
    return changed


def _compute_summary(data: dict[str, Any]) -> dict[str, Any]:
    cap = float(data.get("starting_capital") or 1000.0)
    bets = data.get("bets") or []

    won_c = 0
    lost_c = 0
    pend_c = 0
    void_c = 0
    staked_settled = 0.0
    pending_exposure = 0.0
    potential_pending_payout = 0.0
    net_profit = 0.0
    win_odds_sum = 0.0

    enriched_bets = []
    for b in bets:
        stake = round(float(b.get("stake") or 0.0), 2)
        odds = round(float(b.get("odds") or 1.0), 2)
        st = (b.get("status") or "pending").lower()
        pot_payout = round(stake * odds, 2)
        pot_profit = round(stake * (odds - 1.0), 2)

        if st == "won":
            won_c += 1
            staked_settled += stake
            net_profit += pot_profit
            win_odds_sum += odds
            pnl = pot_profit
            payout = pot_payout
        elif st == "lost":
            lost_c += 1
            staked_settled += stake
            net_profit -= stake
            pnl = -stake
            payout = 0.0
        elif st == "void":
            void_c += 1
            pnl = 0.0
            payout = stake
        else:
            pend_c += 1
            pending_exposure += stake
            potential_pending_payout += pot_payout
            pnl = 0.0
            payout = pot_payout

        enriched_bets.append({
            **b,
            "stake": stake,
            "odds": odds,
            "potential_payout": pot_payout,
            "potential_profit": pot_profit,
            "settled_pnl": round(pnl, 2),
            "payout": round(payout, 2),
        })

    # Sort newest submitted first
    enriched_bets.sort(key=lambda x: (x.get("submitted_at") or "", x.get("id") or ""), reverse=True)

    current_bankroll = round(cap + net_profit, 2)
    available_balance = round(current_bankroll - pending_exposure, 2)
    decided = won_c + lost_c
    win_rate = round(won_c / decided, 4) if decided > 0 else 0.0
    roi_yield = round(net_profit / staked_settled, 4) if staked_settled > 0 else 0.0
    growth_pct = round(net_profit / cap, 4) if cap > 0 else 0.0
    avg_win_odds = round(win_odds_sum / won_c, 2) if won_c > 0 else 0.0

    return {
        "starting_capital": round(cap, 2),
        "currency": data.get("currency", "€"),
        "current_bankroll": current_bankroll,
        "available_balance": available_balance,
        "pending_exposure": round(pending_exposure, 2),
        "potential_pending_payout": round(potential_pending_payout, 2),
        "net_profit": round(net_profit, 2),
        "bankroll_growth_pct": growth_pct,
        "total_staked_settled": round(staked_settled, 2),
        "roi_yield": roi_yield,
        "total_bets": len(bets),
        "won_count": won_c,
        "lost_count": lost_c,
        "pending_count": pend_c,
        "void_count": void_c,
        "win_rate": win_rate,
        "avg_win_odds": avg_win_odds,
        "bets": enriched_bets,
        "deleted_ids": data.get("deleted_ids", []),
        "updated_at": data.get("updated_at", time.time()),
    }


def get_bets_state(force_refresh: bool = False) -> dict[str, Any]:
    with _bets_lock:
        data = _load_raw()
        bets_list = data.get("bets", [])
        lookup = _build_live_match_lookup(bets_list, force_fetch=force_refresh)
        changed = False
        for b in bets_list:
            if _try_settle_bet(b, lookup):
                changed = True
        if changed:
            _save_raw(data)
        return _compute_summary(data)


def set_bankroll_capital(starting_capital: float, currency: str = "€") -> dict[str, Any]:
    with _bets_lock:
        data = _load_raw()
        data["starting_capital"] = round(max(0.0, float(starting_capital)), 2)
        if currency:
            data["currency"] = currency
        _save_raw(data)
        return _compute_summary(data)


def add_user_bet(payload: dict[str, Any]) -> dict[str, Any]:
    with _bets_lock:
        data = _load_raw()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        raw_legs = payload.get("legs") or []
        is_multi = len(raw_legs) >= 2 or bool(payload.get("is_multi"))

        if is_multi and raw_legs:
            clean_legs = []
            comb_odds = 1.0
            sports_set = set()
            for lg in raw_legs:
                lo = round(max(1.01, float(lg.get("odds") or 1.5)), 2)
                comb_odds *= lo
                sp_l = (lg.get("sport") or "football").lower()
                sports_set.add(sp_l)
                clean_legs.append({
                    "espn_id": str(lg.get("espn_id") or ""),
                    "sport": sp_l,
                    "league": lg.get("league") or "Match",
                    "match": lg.get("match") or "Match",
                    "home": lg.get("home") or "",
                    "away": lg.get("away") or "",
                    "pick": lg.get("pick") or "Win",
                    "pick_code": lg.get("pick_code") or "CUSTOM",
                    "odds": lo,
                    "status": (lg.get("status") or "pending").lower(),
                    "score": lg.get("score") or None,
                })
            final_odds = round(max(1.01, float(payload.get("odds") or comb_odds)), 2)
            sport_val = list(sports_set)[0] if len(sports_set) == 1 else "multi"
            new_bet = {
                "id": f"bet_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}",
                "is_multi": True,
                "legs": clean_legs,
                "espn_id": "",
                "sport": sport_val,
                "league": f"🎫 {len(clean_legs)}-Leg Multi",
                "league_name": f"Multi-Bet ({len(clean_legs)} Legs)",
                "match": " + ".join(l["match"] for l in clean_legs),
                "home": "",
                "away": "",
                "kickoff": payload.get("kickoff") or now_str,
                "submitted_at": payload.get("submitted_at") or now_str,
                "pick": " • ".join(f"{l['pick']} (@{l['odds']:.2f})" for l in clean_legs),
                "pick_code": "MULTI",
                "odds": final_odds,
                "stake": round(max(0.01, float(payload.get("stake") or 10.0)), 2),
                "status": (payload.get("status") or "pending").lower(),
                "manual_status": bool(payload.get("status") and payload.get("status") != "pending"),
                "score": None,
            }
        else:
            new_bet = {
                "id": f"bet_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}",
                "is_multi": False,
                "legs": [],
                "espn_id": str(payload.get("espn_id") or ""),
                "sport": (payload.get("sport") or "football").lower(),
                "league": payload.get("league") or "Match",
                "league_name": payload.get("league_name") or payload.get("league") or "Match",
                "match": payload.get("match") or "Custom Match",
                "home": payload.get("home") or "",
                "away": payload.get("away") or "",
                "kickoff": payload.get("kickoff") or now_str,
                "submitted_at": payload.get("submitted_at") or now_str,
                "pick": payload.get("pick") or "Win",
                "pick_code": payload.get("pick_code") or "CUSTOM",
                "odds": round(max(1.01, float(payload.get("odds") or 1.5)), 2),
                "stake": round(max(0.01, float(payload.get("stake") or 10.0)), 2),
                "status": (payload.get("status") or "pending").lower(),
                "manual_status": bool(payload.get("status") and payload.get("status") != "pending"),
                "score": payload.get("score") or None,
            }
        lookup = _build_live_match_lookup()
        _try_settle_bet(new_bet, lookup)
        data.setdefault("bets", []).append(new_bet)
        _save_raw(data)
        return _compute_summary(data)


def combine_user_bets(
    bet_ids: list[str],
    stake: float | None = None,
    odds: float | None = None,
    submitted_at: str | None = None,
) -> dict[str, Any]:
    """Merge 2 or more existing recorded bets into 1 single Multi-Bet (Parlay)."""
    with _bets_lock:
        data = _load_raw()
        all_bets = data.get("bets") or []
        to_merge = [b for b in all_bets if b.get("id") in bet_ids]
        if len(to_merge) < 2:
            return _compute_summary(data)

        legs = []
        comb_odds = 1.0
        for b in to_merge:
            if b.get("legs"):
                for l in b["legs"]:
                    lo = round(max(1.01, float(l.get("odds") or 1.5)), 2)
                    comb_odds *= lo
                    legs.append(l)
            else:
                lo = round(max(1.01, float(b.get("odds") or 1.5)), 2)
                comb_odds *= lo
                legs.append({
                    "espn_id": str(b.get("espn_id") or ""),
                    "sport": (b.get("sport") or "football").lower(),
                    "league": b.get("league") or "Match",
                    "match": b.get("match") or "Match",
                    "home": b.get("home") or "",
                    "away": b.get("away") or "",
                    "pick": b.get("pick") or "Win",
                    "pick_code": b.get("pick_code") or "CUSTOM",
                    "odds": lo,
                    "status": (b.get("status") or "pending").lower(),
                    "score": b.get("score") or None,
                })

        first_b = to_merge[0]
        final_stake = round(max(0.01, float(stake if stake is not None else (first_b.get("stake") or 10.0))), 2)
        final_odds = round(max(1.01, float(odds if odds is not None else comb_odds)), 2)
        final_date = submitted_at or first_b.get("submitted_at") or datetime.now().strftime("%Y-%m-%d %H:%M")
        sports_set = {l.get("sport", "football") for l in legs}
        sport_val = list(sports_set)[0] if len(sports_set) == 1 else "multi"

        multi_bet = {
            "id": f"bet_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}",
            "is_multi": True,
            "legs": legs,
            "espn_id": "",
            "sport": sport_val,
            "league": f"🎫 {len(legs)}-Leg Multi",
            "league_name": f"Multi-Bet ({len(legs)} Legs)",
            "match": " + ".join(l["match"] for l in legs),
            "home": "",
            "away": "",
            "kickoff": first_b.get("kickoff") or final_date,
            "submitted_at": final_date,
            "pick": " • ".join(f"{l['pick']} (@{l['odds']:.2f})" for l in legs),
            "pick_code": "MULTI",
            "odds": final_odds,
            "stake": final_stake,
            "status": "pending",
            "manual_status": False,
            "score": None,
        }
        lookup = _build_live_match_lookup()
        _try_settle_bet(multi_bet, lookup)

        remaining = [b for b in all_bets if b.get("id") not in bet_ids]
        remaining.append(multi_bet)
        data["bets"] = remaining
        _save_raw(data)
        return _compute_summary(data)


def update_user_bet(bet_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    with _bets_lock:
        data = _load_raw()
        for b in data.get("bets", []):
            if b.get("id") == bet_id:
                if "odds" in updates and updates["odds"] is not None:
                    b["odds"] = round(max(1.01, float(updates["odds"])), 2)
                if "stake" in updates and updates["stake"] is not None:
                    b["stake"] = round(max(0.01, float(updates["stake"])), 2)
                if "pick" in updates and updates["pick"]:
                    b["pick"] = str(updates["pick"])
                if "submitted_at" in updates and updates["submitted_at"]:
                    b["submitted_at"] = str(updates["submitted_at"])
                if "status" in updates and updates["status"]:
                    b["status"] = str(updates["status"]).lower()
                    b["manual_status"] = (b["status"] != "pending")
                if "score" in updates and updates["score"] is not None:
                    b["score"] = str(updates["score"])
                break
        _save_raw(data)
        return _compute_summary(data)


def delete_user_bet(bet_id: str) -> dict[str, Any]:
    with _bets_lock:
        data = _load_raw()
        data["bets"] = [b for b in data.get("bets", []) if b.get("id") != bet_id]
        deleted = list(data.get("deleted_ids") or [])
        if bet_id not in deleted:
            deleted.append(bet_id)
        data["deleted_ids"] = deleted[-200:]
        data["updated_at"] = time.time()
        _save_raw(data)
        return _compute_summary(data)


def sync_user_bets(client_backup: dict[str, Any]) -> dict[str, Any]:
    """Merge browser localStorage backup with server state so Render container restarts never lose bets."""
    with _bets_lock:
        data = _load_raw()
        server_bets = {b["id"]: b for b in (data.get("bets") or []) if b.get("id")}
        deleted_set = set(data.get("deleted_ids") or []) | set(client_backup.get("deleted_ids") or [])

        client_ts = float(client_backup.get("updated_at") or 0.0)
        server_ts = float(data.get("updated_at") or 0.0)

        changed = False
        if client_ts > server_ts:
            if client_backup.get("starting_capital") is not None:
                data["starting_capital"] = round(max(0.0, float(client_backup["starting_capital"])), 2)
                changed = True
            if client_backup.get("currency"):
                data["currency"] = str(client_backup["currency"])
                changed = True

        for cb in client_backup.get("bets") or []:
            cid = cb.get("id")
            if not cid or cid in deleted_set:
                continue
            if cid not in server_bets:
                # Strip computed fields before storing
                clean = {k: v for k, v in cb.items() if k not in ("potential_payout", "potential_profit", "settled_pnl", "payout")}
                server_bets[cid] = clean
                changed = True
            elif client_ts > server_ts:
                sb = server_bets[cid]
                for fld in ("odds", "stake", "pick", "submitted_at", "status", "manual_status", "score", "legs", "is_multi"):
                    if fld in cb and cb[fld] != sb.get(fld):
                        sb[fld] = cb[fld]
                        changed = True

        # Remove any bet in deleted_set
        final_list = [b for bid, b in server_bets.items() if bid not in deleted_set]
        if len(final_list) != len(data.get("bets") or []):
            changed = True

        data["bets"] = final_list
        data["deleted_ids"] = list(deleted_set)[-200:]
        lookup = _build_live_match_lookup(data["bets"], force_fetch=False)
        for b in data["bets"]:
            if _try_settle_bet(b, lookup):
                changed = True

        if changed:
            data["updated_at"] = max(time.time(), client_ts)
            _save_raw(data)

        res = _compute_summary(data)
        res["deleted_ids"] = data.get("deleted_ids", [])
        res["updated_at"] = data.get("updated_at", time.time())
        return res
