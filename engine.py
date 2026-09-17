#!/usr/bin/env python3
"""
Debutantes Colombia — detection engine.

Reads matches from the Highlightly football API, finds under-21 first
appearances, and writes them to the Supabase 'debutantes' table.

Usage:
    python engine.py --date 2026-09-14
    python engine.py --from 2026-07-01 --to 2026-09-14
    python engine.py --date 2026-09-14 --dry-run   # prints without saving
    python engine.py --date 2026-09-14 --notify    # also sends Telegram messages
                                                     # for each real debut (off by
                                                     # default — use only for the
                                                     # daily run, never a backfill)
    python engine.py                               # default: full backfill,
                                                     # 2026-07-01 -> today,
                                                     # Primera A then Primera B

Debut definition:
  - A DEBUT is a player's first match with minutesPlayed > 0. Appearing only
    on the bench (min = 0) is NOT a debut.
  - Under-21 players go through two states in the 'debutantes' table:
      "banca"              — seen in a squad, under 21, min = 0 so far.
                              Shown as "solo al banco / por debutar".
      "titular" / "cambio" — min > 0 in some match. This is the real debut;
                              it overwrites any earlier "banca" row for the
                              same player (debutantes is upserted on
                              player_id alone, one row per player).
  - A "banca" player is NOT added to seen_players, so every future match
    re-checks whether they've finally played. Only once a player is a real
    debutante (or turns out to be 21+/DOB-unknown) are they added to
    seen_players and permanently skipped.
  - Requires a UNIQUE constraint on debutantes.player_id in Supabase (the
    upsert target changed from (player_id, debut_date) to player_id, since
    each player now has at most one row).

Quota-saving notes:
  - A player's birthdate, once fetched, is cached forever in dob_cache.json —
    it is never requested from the API twice.
  - A player already in the seen_players table (adult, DOB-unknown, or a
    locked-in real debutante) is never sent to /players/{id} again, on any
    later date. Bench-only players are deliberately excluded from
    seen_players so they keep being re-evaluated.
  - A match, once its lineup + box-score have been fully read, is recorded in
    progress.json. Re-running the same date (e.g. after a quota-exhaustion
    stop) skips already-processed matches entirely — no /lineups or
    /box-score call is repeated. If every match for a given league+date is
    already processed, the /matches list call itself is skipped too.
  - Highlightly's /lineups sometimes returns a null player id (seen for late
    squad additions). When that happens we fall back to the matching
    player's id in /box-score (matched by name) instead of dropping them.

Post-debut tracking:
  - Any debutante who actually played (min > 0) is tracked for their next 5
    appearances with minutes, recorded into the 'post_debut' table. This
    reuses the box-score data already fetched for debut detection — no
    extra Highlightly API calls.
  - Only matches processed from now on are scanned for this. A match already
    marked complete in progress.json from a prior run is never re-opened, so
    post-debut appearances that fell inside an already-backfilled date range
    before this feature existed will not be picked up retroactively.
"""

import os
import sys
import json
import time
import argparse
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from supabase import create_client

# Windows terminals often default stdout to cp1252, which can't encode the
# ★ / → characters this script prints — force UTF-8 so it never crashes on them.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# ─── Credentials ─────────────────────────────────────────────────────────────
load_dotenv()
API_KEY = os.environ["HIGHLIGHTLY_KEY"]
SB_URL  = os.environ["SUPABASE_URL"]
SB_KEY  = os.environ["SUPABASE_KEY"]

# Telegram is optional — a missing token/chat id just disables notifications
# (see send_telegram_notification), it never raises.
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ─── Constants ────────────────────────────────────────────────────────────────
# Dict order is traversal order: Primera A is always processed before Primera B.
LEAGUES    = {204173: "Primera A", 205024: "Primera B"}
SEASON     = 2026
API_BASE   = "https://soccer.highlightly.net"
HEADERS    = {"x-rapidapi-key": API_KEY}
RATE_DELAY = 2   # seconds between Highlightly calls — free plan is 100 req/day,
                 # so this is just politeness, not a hard quota requirement.
LOW_QUOTA_WARN = 5  # print a warning once remaining requests drop to this

BACKFILL_FROM = date(2026, 7, 1)  # default backfill start when no date args given

DOB_CACHE_FILE = Path("dob_cache.json")   # avoids re-fetching birthdates from the API
PROGRESS_FILE  = Path("progress.json")    # avoids re-fetching lineups/box-scores
                                           # for matches already fully processed

# ─── Clients ─────────────────────────────────────────────────────────────────
sb = create_client(SB_URL, SB_KEY)

class QuotaExhausted(RuntimeError):
    """Raised when Highlightly's daily request quota hits 0."""

# ─── Highlightly: rate-limited GET ───────────────────────────────────────────
_last_call = 0.0
_remaining = None  # last-seen value of x-ratelimit-requests-remaining, or None if unknown

def api_get(path, params=None):
    """
    Make one GET request to Highlightly.
    Waits if the last call was less than RATE_DELAY seconds ago, and refuses
    to make further calls once the daily quota is known to be exhausted.
    """
    global _last_call, _remaining

    if _remaining is not None and _remaining <= 0:
        raise QuotaExhausted("Highlightly daily quota exhausted (0 requests remaining). Stopping.")

    wait = RATE_DELAY - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)

    response = requests.get(
        API_BASE + path,
        headers=HEADERS,
        params=params or {},
        timeout=20,
    )
    _last_call = time.time()

    remaining_hdr = response.headers.get("x-ratelimit-requests-remaining")
    if remaining_hdr is not None:
        try:
            _remaining = int(remaining_hdr)
            if _remaining <= LOW_QUOTA_WARN:
                print(f"      [WARN] Highlightly quota low: {_remaining} request(s) remaining today")
        except ValueError:
            pass

    response.raise_for_status()
    return response.json()

# ─── DOB cache ────────────────────────────────────────────────────────────────
def load_dob_cache():
    """Load birthdate cache from disk (empty dict if it doesn't exist yet)."""
    if DOB_CACHE_FILE.exists():
        return json.loads(DOB_CACHE_FILE.read_text())
    return {}

def save_dob_cache(cache):
    """Write the birthdate cache back to disk."""
    DOB_CACHE_FILE.write_text(json.dumps(cache, indent=2))

def _parse_highlightly_dob(raw):
    """Convert Highlightly's 'DD/MM/YYYY' birthDate string to ISO 'YYYY-MM-DD'."""
    if not raw:
        return None
    try:
        d, m, y = raw.split("/")
        return date(int(y), int(m), int(d)).isoformat()
    except (ValueError, AttributeError):
        return None

def get_dob(player_id, cache):
    """
    Return a player's birthdate ('YYYY-MM-DD') or None.
    Checks the local cache first; calls the API only if the player isn't cached yet.
    The result (even None) is stored so we never fetch the same player twice.
    """
    key = str(player_id)
    if key in cache:
        return cache[key]
    data = api_get(f"/players/{player_id}")
    item = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
    raw = item.get("birthDate") or (item.get("profile") or {}).get("birthDate")
    dob = _parse_highlightly_dob(raw)
    cache[key] = dob
    return dob

def load_progress():
    """
    Load match/date completion progress from disk.
    - processed_matches: match ids whose lineup + box-score have already been
      read and decided on — never re-fetched.
    - complete_league_dates: "leagueId:YYYY-MM-DD" keys where every match
      returned by /matches was already fully processed — the /matches list
      call itself is skipped for these on the next run.
    """
    if PROGRESS_FILE.exists():
        raw = json.loads(PROGRESS_FILE.read_text())
        return {
            "processed_matches":     set(raw.get("processed_matches", [])),
            "complete_league_dates": set(raw.get("complete_league_dates", [])),
        }
    return {"processed_matches": set(), "complete_league_dates": set()}

def save_progress(progress):
    """Write match/date completion progress back to disk."""
    PROGRESS_FILE.write_text(json.dumps({
        "processed_matches":     sorted(progress["processed_matches"]),
        "complete_league_dates": sorted(progress["complete_league_dates"]),
    }, indent=2))

def age_at(dob_str, on_date):
    """Return the integer age of a player on a given date, or None if DOB is missing/invalid."""
    if not dob_str:
        return None
    try:
        dob = date.fromisoformat(dob_str)
    except ValueError:
        return None
    a = on_date.year - dob.year
    if (on_date.month, on_date.day) < (dob.month, dob.day):
        a -= 1  # birthday hasn't come yet this year
    return a

# ─── Telegram notifications ───────────────────────────────────────────────────
def send_telegram_notification(row, target_date):
    """
    Send a Telegram message for one real debut (row["role"] is "titular" or
    "cambio" — never called for "banca" rows). Silently does nothing if
    TELEGRAM_TOKEN/TELEGRAM_CHAT_ID aren't set, and never raises — a Telegram
    failure must not break the engine or block a Supabase write.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        age = age_at(row.get("dob"), target_date)
        age_str = f"{age} años" if age is not None else "edad desconocida"
        torneo = "Apertura" if target_date.month <= 6 else "Clausura"
        role_str = "de titular" if row["role"] == "titular" else "entró de cambio"

        lines = [
            "⚽ NUEVO DEBUT — CSA Debut Tracker",
            f"{row['name']}, {age_str}",
            f"{row['club']} · {row['liga']}",
            f"vs {row['rival']} — Fecha {row['jornada']} · {torneo} {target_date.year}",
            f"{row['min']}′ · {role_str}",
        ]
        if row.get("rating") is not None:
            lines.append(f"Nota: {row['rating']}")

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": "\n".join(lines)},
            timeout=10,
        )
    except Exception as e:
        print(f"      [WARN] Telegram notification failed: {e}")

# ─── Supabase helpers ─────────────────────────────────────────────────────────
def load_seen_players():
    """
    Load all player_ids from the seen_players table into a Python set.
    This set is held in memory for the entire run so lookups are instant.
    """
    rows = sb.table("seen_players").select("player_id").execute()
    return {r["player_id"] for r in rows.data}

def flush_seen(records):
    """
    Batch-save a list of {player_id, name, first_seen} dicts to seen_players.
    Uses upsert so re-running the same date is safe (no duplicates).
    """
    if records:
        sb.table("seen_players").upsert(records, on_conflict="player_id").execute()

def save_debutante(row):
    """
    Insert or update one player's row in 'debutantes'. Uses upsert on
    player_id alone — a player has at most one row. This lets a "banca"
    (bench-only) row be overwritten in place once the player graduates to a
    real debut (min > 0), instead of accumulating a second row.
    Requires a UNIQUE constraint on debutantes.player_id in Supabase.
    """
    sb.table("debutantes").upsert(row, on_conflict="player_id").execute()

def load_bench_players():
    """
    Load player_ids currently in the "solo al banco" state: under-21 players
    seen in a squad but with no match where min > 0 yet. These are
    deliberately NOT in seen_players, so every future match re-checks them
    until they either graduate (min > 0 — replaces this row with a real
    debut) or simply keep sitting on the bench.
    """
    rows = sb.table("debutantes").select("player_id").eq("role", "banca").execute()
    return {r["player_id"] for r in rows.data}

def load_tracked_players():
    """
    Build the post-debut tracking set: every player from 'debutantes' whose
    debut had min > 0 (they actually played), minus anyone who already has
    5 rows in 'post_debut' — they're done being tracked.
    Returns {player_id: {"name":.., "debut_date": date, "count": int, "dates": set(...)}}
    """
    debuts = sb.table("debutantes").select("player_id,name,debut_date,min").gt("min", 0).execute().data
    tracked = {}
    for row in debuts:
        tracked[row["player_id"]] = {
            "name":       row["name"],
            "debut_date": date.fromisoformat(row["debut_date"]),
            "count":      0,
            "dates":      set(),
        }
    if not tracked:
        return tracked

    posts = sb.table("post_debut").select("player_id,match_date").execute().data
    for row in posts:
        pid = row["player_id"]
        if pid in tracked:
            tracked[pid]["count"] += 1
            tracked[pid]["dates"].add(row["match_date"])

    return {pid: info for pid, info in tracked.items() if info["count"] < 5}

def save_post_debut(row):
    """
    Insert one post-debut appearance. Uses upsert on (player_id, match_date)
    so re-running a date already recorded is safe — no duplicates.
    """
    sb.table("post_debut").upsert(row, on_conflict="player_id,match_date").execute()

# ─── Helpers ─────────────────────────────────────────────────────────────────
def parse_jornada(round_str):
    """Extract the matchday number from e.g. 'Regular Season - 11' → 11."""
    try:
        return int(round_str.rsplit("-", 1)[-1].strip())
    except (ValueError, IndexError):
        return None

def _flatten(entries):
    """
    Flatten a possibly nested list of player entries. Highlightly's
    initialLineup has been observed both as a flat list of player dicts and
    as a list of rows (grouped by formation line) — handle either shape.
    """
    for entry in entries or []:
        if isinstance(entry, list):
            yield from _flatten(entry)
        else:
            yield entry

# ─── Core: one match ──────────────────────────────────────────────────────────
def process_match(match, league_name, target_date, seen, bench, dob_cache, tracked, dry_run, notify):
    """
    Returns True if the match was fully read (lineup + box-score available,
    debut decision made) — safe to mark as processed and never fetch again.
    Returns False if the match couldn't be fully read yet (e.g. not played
    yet, no lineup data, or a player's id couldn't be resolved at all) — it
    should be retried on a later run.
    """
    mid      = match["id"]
    jornada  = parse_jornada(match.get("round", ""))
    home     = match["homeTeam"]
    away     = match["awayTeam"]
    date_str = target_date.isoformat()
    print(f"    [{mid}]  {home['name']} vs {away['name']}  (Fecha {jornada})")

    # Which team is the rival of which?
    rival_of   = {home["id"]: away["name"], away["id"]: home["name"]}
    team_names = {home["id"]: home["name"],  away["id"]: away["name"]}

    # ── Step 1: Fetch lineups ────────────────────────────────────────────────
    lineups = api_get(f"/lineups/{mid}")
    if not lineups or not (lineups.get("homeTeam") or lineups.get("awayTeam")):
        print("      (no lineup data — match may not have been played yet)")
        return False

    # Build a dict of every player in the squad: {player_id: info}.
    # Highlightly sometimes omits the numeric id for a player in /lineups
    # (observed for late squad additions) — those go into `unresolved` and
    # get a second chance below, matched by name against /box-score.
    players    = {}
    unresolved = []
    for side, fallback_team in (("homeTeam", home), ("awayTeam", away)):
        block = lineups.get(side) or {}
        tid = block.get("id", fallback_team["id"])
        for p in _flatten(block.get("initialLineup")):
            info = {
                "name": p.get("name"), "team_id": tid, "in_xi": True,
                "pos": p.get("position"), "shirt": p.get("number") or p.get("shirtNumber"),
            }
            pid = p.get("id")
            if pid:
                players[pid] = info
            else:
                unresolved.append(info)
        for p in block.get("substitutes") or []:
            info = {
                "name": p.get("name"), "team_id": tid, "in_xi": False,
                "pos": p.get("position"), "shirt": p.get("number") or p.get("shirtNumber"),
            }
            pid = p.get("id")
            if pid:
                players[pid] = info
            else:
                unresolved.append(info)

    if not players and not unresolved:
        # The lineups endpoint returned homeTeam/awayTeam blocks, but with no
        # initialLineup/substitutes entries in either — Highlightly sometimes
        # does this for leagues it hasn't populated player-level data for yet
        # (observed on Primera B). Treat exactly like "no lineup data yet" so
        # this match is retried on a future run instead of being cached as done.
        print("      (lineup present but squad is empty — no player data yet, will retry later)")
        return False

    # ── Step 2: Fetch box score (minutes + rating) ───────────────────────────
    # /box-score returns a LIST of {"team": {...}, "players": [...]} blocks —
    # one per team — unlike /lineups which returns a homeTeam/awayTeam dict.
    stats       = {}
    box_by_name = {}  # name -> id, used to resolve `unresolved` lineup entries
    box = api_get(f"/box-score/{mid}")
    for block in box or []:
        for p in block.get("players") or []:
            pid = p.get("id")
            nm  = p.get("name")
            if nm:
                box_by_name.setdefault(nm, pid)
            if not pid:
                continue
            raw_rating = p.get("matchRating")  # Highlightly sends this as a string, e.g. "7.04"
            stats[pid] = {
                "minutes": p.get("minutesPlayed") or 0,
                "rating":  float(raw_rating) if raw_rating not in (None, "") else None,
                "pos":     p.get("position"),
                "shirt":   p.get("shirtNumber"),
                "is_sub":  p.get("isSubstitute"),
            }

    # Resolve the null-id lineup entries by matching their name in box-score.
    for info in unresolved:
        real_id = box_by_name.get(info["name"])
        if real_id:
            players[real_id] = info
            print(f"      (resolved null lineup id for {info['name']} via box-score -> {real_id})")
        else:
            print(f"      [WARN] {info['name']} has no id in /lineups and no box-score match — skipped this match")

    if not players:
        print("      (no player with a resolvable id — will retry later)")
        return False

    # ── Step 3: Decide debut / bench / post-debut for each player ───────────
    new_debuts = []  # rows to upsert into 'debutantes' — real debuts AND banca sightings
    new_seen   = []  # players now permanently decided (adult/unknown-DOB, or a new real debutante)
    new_posts  = []  # post-debut appearances (tracked players) recorded this match
    n_real = n_grad = n_banca = 0

    for pid, pinfo in players.items():
        pst  = stats.get(pid, {})
        mins = pst.get("minutes", 0)
        tid  = pinfo["team_id"]

        if pid in seen:
            # Adult, DOB-unknown, or already a locked-in real debutante —
            # not a debut candidate. Still check post-debut tracking.
            info = tracked.get(pid)
            if (info and mins > 0 and target_date > info["debut_date"]
                    and info["count"] < 5 and date_str not in info["dates"]):
                new_posts.append({
                    "player_id":  pid,
                    "name":       pinfo["name"],
                    "match_date": date_str,
                    "jornada":    jornada,
                    "rival":      rival_of.get(tid, ""),
                    "min":        mins,
                    "rating":     pst.get("rating"),
                })
                info["dates"].add(date_str)
                info["count"] += 1
                print(f"      ↳  {pinfo['name']} post-debut #{info['count']}/5: {mins}′ vs {rival_of.get(tid, '')}")
            continue

        if pid in bench:
            # Known "solo al banco" player from a previous match.
            if mins <= 0:
                continue  # still hasn't played — recheck again next time
            # ── GRADUATION: first match with min > 0 → this is the real debut ──
            dob  = get_dob(pid, dob_cache)  # already cached from when first benched
            role = "titular" if pinfo["in_xi"] else "cambio"
            debut_row = {
                "player_id": pid, "name": pinfo["name"], "dob": dob,
                "club": team_names.get(tid, ""), "liga": league_name,
                "pos": pinfo.get("pos") or pst.get("pos"),
                "shirt": pinfo.get("shirt") or pst.get("shirt"),
                "debut_date": date_str, "jornada": jornada,
                "rival": rival_of.get(tid, ""), "role": role,
                "min": mins, "rating": pst.get("rating"),
            }
            new_debuts.append(debut_row)
            print(f"      ★  {pinfo['name']} — DEBUTA (was solo al banco) — {role}, {mins}′")
            n_grad += 1
            if notify:
                send_telegram_notification(debut_row, target_date)
            bench.discard(pid)
            new_seen.append({"player_id": pid, "name": pinfo["name"], "first_seen": date_str})
            seen.add(pid)
            tracked[pid] = {"name": pinfo["name"], "debut_date": target_date, "count": 0, "dates": set()}
            continue

        # Never encountered before, on bench or otherwise.
        dob = get_dob(pid, dob_cache)
        a   = age_at(dob, target_date) if dob else None

        if a is None or a >= 21:
            # 21+ (or no birth data) — permanently not a candidate.
            new_seen.append({"player_id": pid, "name": pinfo["name"], "first_seen": date_str})
            seen.add(pid)
            continue

        # ── Under 21, first-ever sighting ─────────────────────────────────
        if mins > 0:
            role = "titular" if pinfo["in_xi"] else "cambio"
            debut_row = {
                "player_id": pid, "name": pinfo["name"], "dob": dob,
                "club": team_names.get(tid, ""), "liga": league_name,
                "pos": pinfo.get("pos") or pst.get("pos"),
                "shirt": pinfo.get("shirt") or pst.get("shirt"),
                "debut_date": date_str, "jornada": jornada,
                "rival": rival_of.get(tid, ""), "role": role,
                "min": mins, "rating": pst.get("rating"),
            }
            new_debuts.append(debut_row)
            print(f"      ★  {pinfo['name']} ({a} años) — {role}, {mins}′")
            n_real += 1
            if notify:
                send_telegram_notification(debut_row, target_date)
            new_seen.append({"player_id": pid, "name": pinfo["name"], "first_seen": date_str})
            seen.add(pid)
            tracked[pid] = {"name": pinfo["name"], "debut_date": target_date, "count": 0, "dates": set()}
        else:
            new_debuts.append({
                "player_id": pid, "name": pinfo["name"], "dob": dob,
                "club": team_names.get(tid, ""), "liga": league_name,
                "pos": pinfo.get("pos") or pst.get("pos"),
                "shirt": pinfo.get("shirt") or pst.get("shirt"),
                "debut_date": date_str, "jornada": jornada,
                "rival": rival_of.get(tid, ""), "role": "banca",
                "min": 0, "rating": pst.get("rating"),
            })
            print(f"      ⏳  {pinfo['name']} ({a} años) — solo al banco (por debutar)")
            n_banca += 1
            bench.add(pid)
            # Deliberately NOT added to `seen` — must be re-checked next time.

    # ── Step 4: Save to Supabase ─────────────────────────────────────────────
    if not dry_run:
        for row in new_debuts:
            save_debutante(row)
        for row in new_posts:
            save_post_debut(row)
        flush_seen(new_seen)   # one batch write for all newly-decided players

    label = "DRY RUN — " if dry_run else ""
    print(f"      → {label}{n_real} debut(s), {n_grad} graduation(s) from banca, "
          f"{n_banca} new banca sighting(s), {len(new_posts)} post-debut appearance(s)")
    return True


# ─── Core: one league on one date ─────────────────────────────────────────────
def process_league_date(league_id, league_name, target_date, seen, bench, dob_cache, tracked, progress, dry_run, notify):
    date_str = target_date.isoformat()
    key = f"{league_id}:{date_str}"

    if key in progress["complete_league_dates"]:
        print(f"  {date_str}: already fully processed — skipping (0 API calls)")
        return

    try:
        data = api_get("/matches", {
            "leagueId": league_id,
            "season":   SEASON,
            "date":     date_str,
        })
    except QuotaExhausted:
        raise
    except Exception as e:
        print(f"  {date_str}: [ERROR] fetching match list: {e}")
        return

    matches = data.get("data", []) if isinstance(data, dict) else (data or [])
    print(f"\n  {date_str}: {len(matches)} match(es) found")

    all_complete = True
    for match in matches:
        mid = str(match["id"])
        if mid in progress["processed_matches"]:
            print(f"    [{mid}] already processed — skipping (0 API calls)")
            continue
        try:
            completed = process_match(match, league_name, target_date, seen, bench, dob_cache, tracked, dry_run, notify)
        except QuotaExhausted:
            raise  # stop everything — no point trying more matches/leagues/dates
        except Exception as e:
            print(f"      [ERROR] match {mid}: {e}")
            all_complete = False
            continue

        if completed:
            if not dry_run:
                progress["processed_matches"].add(mid)
        else:
            all_complete = False  # not played yet — retry this date later

    if all_complete and not dry_run:
        progress["complete_league_dates"].add(key)


# ─── CLI ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Detect Colombian league debutantes and save them to Supabase."
    )
    grp = ap.add_mutually_exclusive_group(required=False)
    grp.add_argument("--date",      metavar="YYYY-MM-DD",
                     help="Process a single date")
    grp.add_argument("--from",      dest="from_date", metavar="YYYY-MM-DD",
                     help="Start of a date range (pair with --to)")
    ap.add_argument( "--to",        metavar="YYYY-MM-DD",
                     help="End of a date range")
    ap.add_argument( "--dry-run",   action="store_true",
                     help="Print debutantes without writing anything to Supabase")
    ap.add_argument( "--league",    action="append", type=int, metavar="LEAGUE_ID",
                     help="Restrict to this league id (repeatable). Default: all leagues in LEAGUES.")
    ap.add_argument( "--notify",    action="store_true",
                     help="Send a Telegram message for each real debut found. Off by default so "
                          "backfills/rebuilds don't spam — pass this only for the daily run.")
    args = ap.parse_args()

    leagues_to_run = {lid: name for lid, name in LEAGUES.items() if not args.league or lid in args.league}
    if not leagues_to_run:
        ap.error(f"--league didn't match any known league id: {list(LEAGUES)}")

    # Build list of dates to process
    if args.date:
        dates = [date.fromisoformat(args.date)]
    elif args.from_date:
        if not args.to:
            ap.error("--to is required when using --from")
        d, end = date.fromisoformat(args.from_date), date.fromisoformat(args.to)
        dates = []
        while d <= end:
            dates.append(d)
            d += timedelta(days=1)
    else:
        # No date args at all → default to the full second-half-of-2026 backfill.
        d, end = BACKFILL_FROM, date.today()
        dates = []
        while d <= end:
            dates.append(d)
            d += timedelta(days=1)
        print(f"No --date/--from given — defaulting to backfill {BACKFILL_FROM.isoformat()} → {end.isoformat()}")

    print(f"Processing {len(dates)} date(s) × {len(leagues_to_run)} league(s)…")
    if args.dry_run:
        print("*** DRY RUN — nothing will be written to Supabase ***\n")
    if args.notify:
        print("*** --notify is ON — a Telegram message will be sent for each real debut ***\n")

    dob_cache = load_dob_cache()
    seen      = load_seen_players()
    bench     = load_bench_players()
    progress  = load_progress()
    tracked   = load_tracked_players()
    print(f"Loaded {len(seen)} already-seen player(s) from Supabase.")
    print(f"Loaded {len(bench)} player(s) currently 'solo al banco' (under 21, not yet played).")
    print(f"Loaded {len(progress['processed_matches'])} already-processed match(es) from progress.json.")
    print(f"Loaded {len(tracked)} player(s) under post-debut tracking (<5 appearances recorded).")

    try:
        # Primera A fully before Primera B (LEAGUES dict order), across the
        # whole date range — so a quota stop leaves one league fully covered
        # rather than both leagues half-covered.
        for league_id, league_name in leagues_to_run.items():
            print(f"\n{'='*52}")
            print(f"  {league_name}")
            print(f"{'='*52}")
            for d in dates:
                process_league_date(league_id, league_name, d, seen, bench, dob_cache, tracked, progress,
                                     dry_run=args.dry_run, notify=args.notify)
    except QuotaExhausted as e:
        print(f"\n[STOPPED] {e}")
        print("Run again once the daily quota resets to finish the remaining date(s)/league(s).")
    finally:
        # Always save the caches, even if something crashed halfway through
        save_dob_cache(dob_cache)
        save_progress(progress)
        print("\nDOB cache saved to dob_cache.json")
        print("Progress saved to progress.json")


if __name__ == "__main__":
    main()
