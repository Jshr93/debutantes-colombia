#!/usr/bin/env python3
"""
Debutantes Colombia — detection engine.

Reads matches from the Highlightly football API, finds under-21 first
appearances, and writes them to the Supabase 'debutantes' table.

Usage:
    python engine.py --date 2026-09-14
    python engine.py --from 2026-07-01 --to 2026-09-14
    python engine.py --date 2026-09-14 --dry-run   # prints without saving
    python engine.py                               # default: full backfill,
                                                     # 2026-07-01 -> today,
                                                     # Primera A then Primera B

Quota-saving notes:
  - A player's birthdate, once fetched, is cached forever in dob_cache.json —
    it is never requested from the API twice.
  - A player already in the seen_players table is never sent to /players/{id}
    again, on any later date.
  - A match, once its lineup + box-score have been fully read, is recorded in
    progress.json. Re-running the same date (e.g. after a quota-exhaustion
    stop) skips already-processed matches entirely — no /lineups or
    /box-score call is repeated. If every match for a given league+date is
    already processed, the /matches list call itself is skipped too.

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
    Insert one debutante row. Uses upsert on (player_id, debut_date)
    so re-running is safe — it will just overwrite with the same data.
    """
    sb.table("debutantes").upsert(row, on_conflict="player_id,debut_date").execute()

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
def process_match(match, league_name, target_date, seen, dob_cache, tracked, dry_run):
    """
    Returns True if the match was fully read (lineup + box-score available,
    debut decision made) — safe to mark as processed and never fetch again.
    Returns False if the match couldn't be fully read yet (e.g. not played
    yet, no lineup data) — it should be retried on a later run.
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

    # Build a dict of every player in the squad: {player_id: info}
    players = {}
    for side, fallback_team in (("homeTeam", home), ("awayTeam", away)):
        block = lineups.get(side) or {}
        tid = block.get("id", fallback_team["id"])
        for p in _flatten(block.get("initialLineup")):
            pid = p.get("id")
            if not pid:
                continue
            players[pid] = {
                "name": p.get("name"), "team_id": tid, "in_xi": True,
                "pos": p.get("position"), "shirt": p.get("number") or p.get("shirtNumber"),
            }
        for p in block.get("substitutes") or []:
            pid = p.get("id")
            if not pid:
                continue
            players[pid] = {
                "name": p.get("name"), "team_id": tid, "in_xi": False,
                "pos": p.get("position"), "shirt": p.get("number") or p.get("shirtNumber"),
            }

    if not players:
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
    stats = {}
    box = api_get(f"/box-score/{mid}")
    for block in box or []:
        for p in block.get("players") or []:
            pid = p.get("id")
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

    # ── Step 3: Decide who is a debutante ────────────────────────────────────
    new_debuts = []
    new_seen   = []  # everyone new (any age) — recorded so we skip them next time
    new_posts  = []  # post-debut appearances (tracked players) recorded this match

    for pid, pinfo in players.items():
        if pid in seen:
            # Not a new debut. If they're a tracked player, check whether
            # this match is one of their next post-debut appearances.
            info = tracked.get(pid)
            if info and target_date > info["debut_date"] and info["count"] < 5 and date_str not in info["dates"]:
                pst  = stats.get(pid, {})
                mins = pst.get("minutes", 0)
                if mins > 0:
                    tid = pinfo["team_id"]
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
            continue  # already appeared in a previous run — definitely not a debut

        dob = get_dob(pid, dob_cache)
        a   = age_at(dob, target_date) if dob else None

        # Add to seen regardless of age — we never want to check this player again
        new_seen.append({"player_id": pid, "name": pinfo["name"], "first_seen": date_str})
        seen.add(pid)

        if a is None or a >= 21:
            continue  # 21 or older (or no birth data) — not a debutante

        # ── Under 21, first time seen → it's a debut ─────────────────────────
        pst  = stats.get(pid, {})
        mins = pst.get("minutes", 0)

        if pinfo["in_xi"]:
            role = "titular"
        elif mins > 0:
            role = "cambio"  # came on from bench
        else:
            role = "banca"   # listed but didn't play

        tid = pinfo["team_id"]
        new_debuts.append({
            "player_id":  pid,
            "name":       pinfo["name"],
            "dob":        dob,
            "club":       team_names.get(tid, ""),
            "liga":       league_name,
            "pos":        pinfo.get("pos") or pst.get("pos"),
            "shirt":      pinfo.get("shirt") or pst.get("shirt"),
            "debut_date": date_str,
            "jornada":    jornada,
            "rival":      rival_of.get(tid, ""),
            "role":       role,
            "min":        mins,
            "rating":     pst.get("rating"),
        })
        print(f"      ★  {pinfo['name']} ({a} años) — {role}, {mins}′")

        if mins > 0:
            # Start post-debut tracking immediately (in-memory) so later
            # matches in this same run — e.g. a multi-date backfill, not
            # just the daily single-date cron — also pick up this player's
            # next appearances, instead of only from the following run.
            tracked[pid] = {"name": pinfo["name"], "debut_date": target_date, "count": 0, "dates": set()}

    # ── Step 4: Save to Supabase ─────────────────────────────────────────────
    if not dry_run:
        for row in new_debuts:
            save_debutante(row)
        for row in new_posts:
            save_post_debut(row)
        flush_seen(new_seen)   # one batch write for all new players

    label = "DRY RUN — " if dry_run else ""
    print(f"      → {label}{len(new_debuts)} debut(s) found, {len(new_posts)} post-debut appearance(s) in this match")
    return True


# ─── Core: one league on one date ─────────────────────────────────────────────
def process_league_date(league_id, league_name, target_date, seen, dob_cache, tracked, progress, dry_run):
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
            completed = process_match(match, league_name, target_date, seen, dob_cache, tracked, dry_run)
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

    dob_cache = load_dob_cache()
    seen      = load_seen_players()
    progress  = load_progress()
    tracked   = load_tracked_players()
    print(f"Loaded {len(seen)} already-seen player(s) from Supabase.")
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
                process_league_date(league_id, league_name, d, seen, dob_cache, tracked, progress, dry_run=args.dry_run)
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
