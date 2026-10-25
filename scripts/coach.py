#!/usr/bin/env python3
"""dota-coach CLI.

  coach.py games   [--account ID] [--n 20]                 recent games (replays available ~14 days)
  coach.py peers   MATCH [--account ID] [--k 6] [--days 12] [--min-rank 72]
  coach.py analyze MATCH [--account ID | --hero HERO_ID]    one player's metrics as JSON
  coach.py timeline MATCH [--account ID | --hero HERO_ID]   minute-by-minute text digest of one player's game
  coach.py compare MATCH [MATCH ...] [--account ID] [--k 6] [--min-rank 72] [--role auto|any|core|support] [--lane auto|any|safe|mid|off] [--peers id,id,...]
                                                          full report: you vs high-MMR players on the
                                                          same hero in similar drafts (markdown + PNG)
"""
import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import odota  # noqa: E402
from analyze import analyze, role_of, PHASES, ZONES  # noqa: E402
import mapdraw  # noqa: E402

_acct = odota.env_setting("DOTA_COACH_ACCOUNT")
DEFAULT_ACCOUNT = int(_acct) if _acct else None


T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:5.0f}s]", *a, file=sys.stderr, flush=True)


def fmt_t(s):
    if s is None:
        return "-"
    s = int(round(s))
    return f"{'-' if s < 0 else ''}{abs(s) // 60}:{abs(s) % 60:02d}"


# ---------------------------------------------------------------- games
def cmd_games(a):
    H = odota.hero_names()
    rows = odota.api(f"players/{a.account}/matches", limit=a.n, significant=0,
                     project=["hero_id", "player_slot", "radiant_win", "kills", "deaths", "assists",
                              "duration", "start_time", "game_mode", "version", "average_rank"])
    now = time.time()
    print(f"{'match_id':>11}  {'age':>5}  {'hero':<18} {'res':<4} {'K/D/A':<9} {'dur':>5}  mode  replay")
    for r in rows:
        win = (r["player_slot"] < 128) == r["radiant_win"]
        age = (now - r["start_time"]) / 86400
        print(f"{r['match_id']:>11}  {age:4.1f}d  {H.get(r['hero_id'], r['hero_id']):<18} {'W' if win else 'L':<4} "
              f"{r['kills']}/{r['deaths']}/{r['assists']:<5} {fmt_t(r['duration']):>5}  "
              f"{'turbo' if r['game_mode'] == 23 else r['game_mode']:<5} {'yes' if age < 13 else 'expired?'}")


# ---------------------------------------------------------------- peers
def draft_of(m, account=None, hero=None):
    me = None
    for p in m["players"]:
        if (account and p.get("account_id") == account) or (hero and p["hero_id"] == hero):
            me = p
    if me is None:
        raise SystemExit("player not in match")
    rad = me["player_slot"] < 128
    allies = [p["hero_id"] for p in m["players"] if (p["player_slot"] < 128) == rad and p is not me]
    enemies = [p["hero_id"] for p in m["players"] if (p["player_slot"] < 128) != rad]
    return me["hero_id"], allies, enemies


_SEARCH = {}


def find_peers(match_id, account, k=6, days=12, min_rank=75, exclude_account=True):
    m = odota.match(match_id)
    hero, allies, enemies = draft_of(m, account=account)
    key = (hero, days, min_rank)
    if key not in _SEARCH:
        _SEARCH[key] = search_hero(hero, days, min_rank)
    return rank_peers(match_id, hero, allies, enemies, _SEARCH[key], days, min_rank)


def search_hero(hero, days, min_rank):
    """All recent turbo games with this hero (OpenDota explorer, 2-day windows)."""
    hi = odota.latest_match_id()
    win = 2 * odota.IDS_PER_DAY  # the explorer times out on much larger match_id ranges

    def window(top):
        sql = (f"select match_id, start_time, radiant_win, radiant_team, dire_team, avg_rank_tier, duration "
               f"from public_matches where match_id > {top - win} and match_id <= {top} and game_mode = 23 "
               f"and avg_rank_tier >= {min_rank} and duration >= 600 "
               f"and ({hero} = any(radiant_team) or {hero} = any(dire_team))")
        for _ in range(3):
            try:
                return odota.explorer(sql)
            except Exception as e:  # noqa: BLE001
                err = e
                time.sleep(5)
        log(f"  explorer window failed: {err}")
        return []

    tops = [hi - i * win for i in range(max(1, round(days * odota.IDS_PER_DAY / win)))]
    with ThreadPoolExecutor(3) as ex:
        return [r for rs in ex.map(window, tops) for r in rs]


def rank_peers(match_id, hero, allies, enemies, rows, days, min_rank):
    A, E = set(allies), set(enemies)
    cands = []
    for r in rows:
        if r["match_id"] == match_id:
            continue
        rad = hero in r["radiant_team"]
        mine = set(r["radiant_team"] if rad else r["dire_team"]) - {hero}
        theirs = set(r["dire_team"] if rad else r["radiant_team"])
        sa, se = len(mine & A), len(theirs & E)
        cands.append({"match_id": r["match_id"], "sim": sa + se, "same_allies": sa, "same_enemies": se,
                      "win": r["radiant_win"] == rad, "rank": r["avg_rank_tier"], "duration": r["duration"],
                      "start_time": r["start_time"]})
    cands.sort(key=lambda c: (-c["sim"], -c["rank"], -c["match_id"]))
    hist = {}
    for c in cands:
        hist[c["sim"]] = hist.get(c["sim"], 0) + 1
    log(f"{len(rows)} turbo games (avg rank >= {min_rank}) with hero {hero} in last {days} days; "
        f"draft-overlap histogram: {dict(sorted(hist.items(), reverse=True))}")
    return hero, allies, enemies, cands


def cmd_peers(a):
    H = odota.hero_names()
    hero, allies, enemies, cands = find_peers(a.match, a.account, a.k, a.days, a.min_rank)
    print(f"you: {H[hero]} | allies: {', '.join(H[h] for h in allies)} | vs: {', '.join(H[h] for h in enemies)}")
    for c in cands[: max(a.k * 3, 15)]:
        print(f"{c['match_id']}  sim={c['sim']} (allies {c['same_allies']}, enemies {c['same_enemies']})  "
              f"{'W' if c['win'] else 'L'}  rank {c['rank']}  {fmt_t(c['duration'])}")


# ---------------------------------------------------------------- analysis
def load(match_id, account=None, hero=None):
    m = odota.match(match_id)
    rep = odota.replay(match_id, m)
    return analyze(rep, m, account_id=account, hero_id=hero)


def cmd_timeline(a):
    print(load(a.match, a.account if not a.hero else None, a.hero)["timeline"])


def cmd_analyze(a):
    r = load(a.match, a.account if not a.hero else None, a.hero)
    r.pop("path")
    print(json.dumps(r, indent=1))


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def rng(xs):
    xs = [x for x in xs if x is not None]
    return f"{min(xs):g}–{max(xs):g}" if xs else "-"


ROLE = {1: "safe", 2: "mid", 3: "off", 4: "jungle"}
MODES = {23: "Turbo", 22: "All Pick", 2: "Captains Mode", 4: "Single Draft", 3: "Random Draft", 18: "Ability Draft"}


def position(r):
    """'pos 3 offlane core' from GPM-rank role + early-game lane (approximate)."""
    lane, role = r.get("lane_role"), r.get("role")  # lane: where they spent most of 0-5 min
    pos = {("core", "safe"): 1, ("core", "mid"): 2, ("core", "off"): 3,
           ("support", "off"): 4, ("support", "safe"): 5}.get((role, lane))
    lane_s = {"safe": "safe lane", "mid": "mid", "off": "offlane", "jungle": "jungle"}.get(lane, "?")
    return f"{'pos ' + str(pos) + ' ' if pos else ''}{lane_s} {role or '?'}"


def mode(r):
    return MODES.get(r.get("game_mode"), f"mode {r.get('game_mode')}") if r.get("game_mode") is not None else "Turbo"


def summarize(rs, f):
    c = {}
    for r in rs:
        c[f(r)] = c.get(f(r), 0) + 1
    return ", ".join(f"{k} ×{v}" if len(rs) > 1 else k for k, v in sorted(c.items(), key=lambda x: -x[1]))


class RoleMismatch(Exception):
    pass


ROLE_SEEN = {}  # role -> count, over every candidate whose role was checked


LANE_SEEN = {}  # early-game lane -> count, over every role-matched candidate that was parsed


def load_peer(mid, hero, want):
    """want: (role, lane) or None. Role is checked from basic match data first (cheap); lane needs the parsed
    replay, so lane mismatches cost a download but no extra OpenDota requests."""
    want_role, want_lane = want if isinstance(want, tuple) else (want, None)
    m = odota.match_basic(mid)
    if (m.get("duration") or 0) < 900:
        raise RoleMismatch("short")  # abandons / stomps under 15 min say little about how to play
    rank, role = role_of(m, hero)
    if role:
        ROLE_SEEN[role] = ROLE_SEEN.get(role, 0) + 1
    if want_role and role and role != want_role:
        raise RoleMismatch(role)
    rec = load(mid, None, hero)
    lane = rec.get("lane_role")
    if lane:
        LANE_SEEN[lane] = LANE_SEEN.get(lane, 0) + 1
    if want_lane and lane and lane != want_lane:
        raise RoleMismatch(lane)
    return rec


def gather_peers(jobs, k, hero, want_roles=None):
    """jobs: {source_match: [candidate dicts]}. Fetch up to k peers per source match (parallel).
    want_roles: {source_match: "core"/"support"/None} keeps only peers who played that role."""
    want_roles = want_roles or {}
    got = {src: [] for src in jobs}
    queue = {src: list(c) for src, c in jobs.items()}
    seen = set(jobs)
    inflight = {}
    with ThreadPoolExecutor(5) as ex:
        def top_up():
            for src in jobs:
                busy = sum(1 for s in inflight.values() if s[0] == src)
                while len(got[src]) + busy < k and queue[src]:
                    c = queue[src].pop(0)
                    if c["match_id"] in seen:
                        continue
                    seen.add(c["match_id"])
                    inflight[ex.submit(load_peer, c["match_id"], hero, want_roles.get(src))] = (src, c)
                    busy += 1
        top_up()
        while inflight:
            f = next(as_completed(list(inflight)))
            src, c = inflight.pop(f)
            try:
                r = f.result()
                r["source_match"], r["sim"] = src, c["sim"]
                got[src].append(r)
                log(f"  ok {c['match_id']} (for {src}: {len(got[src])}/{k})")
            except RoleMismatch:
                pass
            except Exception as e:  # noqa: BLE001
                log(f"  skip {c['match_id']}: {e}")
            top_up()
    return [p for src in jobs for p in got[src][:k]]


def cmd_compare(a):
    H = odota.hero_names()
    matches = a.match
    log(f"analysing your game(s) {matches} ...")
    with ThreadPoolExecutor(4) as ex:
        mine = list(ex.map(lambda mid: load(mid, a.account), matches))
    hero = mine[0]["hero_id"]
    if any(r["hero_id"] != hero for r in mine):
        raise SystemExit("all matches must be on the same hero")
    drafts = {}
    for mid in matches:
        m = odota.match(mid)
        drafts[mid] = draft_of(m, account=a.account)
    if a.peers:
        jobs = {matches[0]: [{"match_id": int(x), "sim": None} for x in a.peers.split(",")]}
    else:
        jobs = {}
        for mid in matches:
            _, _, _, cands = find_peers(mid, a.account, a.k, a.days, a.min_rank)
            jobs[mid] = [c for c in cands if c["match_id"] not in matches][: a.k * (20 if a.lane != "any" else 12 if a.role != "any" else 3)]
    log(f"fetching up to {a.k} peer games per match (OpenDota parse + replay download) ...")
    want = {}
    for r in mine:
        want[r["match_id"]] = ((r.get("role") if a.role == "auto" else None if a.role == "any" else a.role),
                               (r.get("lane_role") if a.lane == "auto" else None if a.lane == "any" else a.lane))
    if a.peers and a.role == "auto":  # all supplied peers are pooled under the first game; use your usual role
        rs_ = [r.get("role") for r in mine if r.get("role")]
        ls_ = [r.get("lane_role") for r in mine if r.get("lane_role")]
        want = {matches[0]: (max(set(rs_), key=rs_.count) if rs_ else None,
                             max(set(ls_), key=ls_.count) if ls_ and a.lane == "auto" else
                             (None if a.lane in ("any", "auto") else a.lane))}
    log(f"role/lane filter: {want}")
    peers = gather_peers(jobs, a.k, hero, want)
    if not peers:
        raise SystemExit("no peer games could be fetched")

    multi = len(mine) > 1
    tag = matches[0] if not multi else f"{matches[0]}+{len(matches) - 1}"
    out_dir = os.path.join(odota.CACHE, "reports")
    os.makedirs(out_dir, exist_ok=True)
    png = os.path.join(out_dir, f"{tag}_heatmap.png")
    panels = []
    for lo, hi in [(0, 600), (600, 1200), (1200, 99999)]:
        pts = lambda rs: [(x, y) for r in rs for t, x, y in r["path"] if lo <= t < hi]  # noqa: E731
        dts = lambda rs: [(d["x"], d["y"]) for r in rs for d in r["deaths"] if lo <= d["t"] < hi]  # noqa: E731
        if pts(mine):
            panels.append([(pts(mine), dts(mine)), (pts(peers), dts(peers))])
    mapdraw.render(png, panels)

    L = []
    w = L.append
    if multi:
        w(f"# {H[hero]} — {len(mine)} of your games vs {len(peers)} peer games")
    else:
        me = mine[0]
        w(f"# {H[hero]} — match {me['match_id']} ({me['team']}, {'WIN' if me['win'] else 'LOSS'}, {me['duration_min']} min)")
    w(f"\n**Mode:** {summarize(mine, mode)} (peers: {summarize(peers, mode)}). "
      f"**Your role:** {summarize(mine, position)}. **Peers' role:** {summarize(peers, position)}.  \n"
      f"_Role = team net-GPM rank (1–3 core, 4–5 support) + the lane you spent most of 0–5 min in; positions are approximate._")
    w("\n## Your games")
    w("| match | mode | side | result | K/D/A | dur | role (team GPM rank) | allies | enemies |")
    w("|---|---|---|---|---|---|---|---|---|")
    for r in mine:
        _, al, en = drafts[r["match_id"]]
        w(f"| {r['match_id']} | {mode(r)} | {r['team']} | {'W' if r['win'] else 'L'} | {'/'.join(map(str, r['kda']))} | "
          f"{r['duration_min']}m | {position(r)} ({r.get('team_gpm_rank')}) | "
          f"{', '.join(H[h] for h in al)} | {', '.join(H[h] for h in en)} |")
    w(f"\n## Peer games ({len(peers)} turbo games, avg rank ≥ {a.min_rank}, same hero, most similar drafts)")
    if ROLE_SEEN:
        tot_ = sum(ROLE_SEEN.values())
        w(f"Peers are filtered to the role you played in each game (role = team GPM rank: 1–3 core, 4–5 support; "
          f"`--role`). Of {tot_} high-MMR {H[hero]} candidates checked, "
          + ", ".join(f"{100 * v // tot_}% played {k}" for k, v in sorted(ROLE_SEEN.items(), key=lambda x: -x[1])) + ".")
        my_roles = [r.get("role") for r in mine]
        common = max(ROLE_SEEN, key=ROLE_SEEN.get)
        if my_roles.count(common) < len(my_roles) / 2:
            w(f"**Note:** you mostly played {H[hero]} as {max(set(my_roles), key=my_roles.count)}, which is the "
              f"less common choice among strong players ({common} is the norm).")
    if LANE_SEEN:
        tot_ = sum(LANE_SEEN.values())
        w(f"Peers are also filtered to your early-game lane (`--lane`). Of {tot_} role-matched candidates parsed, "
          + ", ".join(f"{100 * v // tot_}% laned {k}" for k, v in sorted(LANE_SEEN.items(), key=lambda x: -x[1])) + ".")
    if ROLE_SEEN or LANE_SEEN:
        w("")
    w("| match | for your game | draft overlap | mode | result | K/D/A | dur | role (GPM rank) |")
    w("|---|---|---|---|---|---|---|---|")
    for p in peers:
        w(f"| {p['match_id']} | {p['source_match']} | {p['sim']} | {mode(p)} | {'W' if p['win'] else 'L'} | "
          f"{'/'.join(str(x) for x in p['kda'])} | {p['duration_min']}m | {position(p)} ({p.get('team_gpm_rank')}) |")
    pw = sum(1 for p in peers if p["win"])
    w(f"\nPeers won {pw}/{len(peers)}; you won {sum(1 for r in mine if r['win'])}/{len(mine)}.")

    fmt = lambda v: f"{v:g}" if isinstance(v, (int, float)) else str(v)  # noqa: E731
    you_hdr = "you (median) | you (range)" if multi else "you"

    def header(first="metric"):
        w(f"| {first} | {you_hdr} | peers (median) | peers (range) | peer wins (median) |")
        w("|---|---|---|---|---|" + ("---|" if multi else ""))

    def row(label, f):
        """f(r) -> value for one game record."""
        mv = [f(r) for r in mine]
        pv = [f(p) for p in peers]
        wv = [f(p) for p in peers if p["win"]]
        m = med(mv)
        you = (f"{fmt(m) if m is not None else '-'} | {rng(mv)}" if multi else (fmt(m) if m is not None else "-"))
        pm, wm = med(pv), med(wv)
        w(f"| {label} | {you} | {fmt(pm) if pm is not None else '-'} | {rng(pv)} | {fmt(wm) if wm is not None else '-'} |")

    per_min = lambda r, k: round(r[k] / r["duration_min"]) if r.get(k) is not None else None  # noqa: E731
    w("\n## Headline numbers")
    header()
    row("deaths per 10 min", lambda r: round(10 * (r["kda"][1] or 0) / r["duration_min"], 1))
    row("kills+assists per 10 min", lambda r: round(10 * ((r["kda"][0] or 0) + (r["kda"][2] or 0)) / r["duration_min"], 1))
    row("GPM", lambda r: r["gpm"])
    row("XPM", lambda r: r["xpm"])
    row("hero dmg / min", lambda r: per_min(r, "hero_damage"))
    row("tower dmg / min", lambda r: per_min(r, "tower_damage"))
    for mm in ("5m", "10m", "15m", "20m"):
        for key in ("nw", "lh", "lvl"):
            row(f"{key} @ {mm}", lambda r, mm=mm, key=key: r["econ"].get(mm, {}).get(key))
    row("fight presence %", lambda r: r["fight_presence_pct"])
    row("teleports / 10 min", lambda r: round(10 * r["teleports"] / r["duration_min"], 1))
    row("deaths with no ally within 1500, per 10 min",
        lambda r: round(10 * sum(1 for d in r["deaths"] if d["allies_near"] == 0) / r["duration_min"], 1))
    row("deaths on enemy half, per 10 min",
        lambda r: round(10 * sum(1 for d in r["deaths"] if d["x"] + d["y"] > 0) / r["duration_min"], 1))

    if all(r.get("laning") for r in mine) and any(p.get("laning") for p in peers):
        LE = mine[0]["laning"]["lane_end_s"]
        w(f"\n## Laning (0:00–{fmt_t(LE)})")
        for r in mine:
            l_ = r["laning"]
            w(f"- you {r['match_id']}: vs {', '.join(l_['opponents']) or '?'}"
              f"{' with ' + ', '.join(l_['partners']) if l_['partners'] else ' (solo)'}")
        header()
        Lg = lambda key, i=None: (lambda r: (r.get("laning") or {}).get(key) if i is None  # noqa: E731
                                  else ((r.get("laning") or {}).get(key) or [None, None])[i])
        row("last hits @ 2:00", Lg("lh_dn_2m", 0))
        row("last hits @ 4:00", Lg("lh_dn_4m", 0))
        row(f"last hits @ {fmt_t(LE)}", Lg("lh_dn_end", 0))
        row(f"denies @ {fmt_t(LE)}", Lg("lh_dn_end", 1))
        row("enemy creeps dying within 600 of you", Lg("nearby_enemy_creep_deaths"))
        row("…of which you last-hit %", Lg("lh_pct_of_nearby_enemy_creeps"))
        row("allied creeps dying near you: denied %", Lg("deny_pct_of_nearby_ally_creeps"))
        row("hero damage dealt / min", Lg("hero_dmg_dealt_per_min"))
        row("…of which right-clicks / min", Lg("attack_dmg_on_heroes_per_min"))
        row("spell casts / min", Lg("spell_casts_per_min"))
        row("spell casts that hit a hero %", Lg("spell_casts_hit_hero_pct"))
        row("damage taken from heroes / min", Lg("hero_dmg_taken_per_min"))
        row("damage taken from lane creeps / min", Lg("creep_dmg_taken_per_min"))
        row("damage taken from towers / min", Lg("tower_dmg_taken_per_min"))
        row("regen / stick uses", Lg("regen_uses"))
        row("average HP %", Lg("avg_hp_pct"))
        row("time below 30% HP %", Lg("low_hp_time_pct"))
        row("deaths", Lg("deaths"))
        row("ahead of your own creep front, units (median; + = in front)", Lg("ahead_of_own_creeps_median"))
        row("time >150 units in front of your creeps %", Lg("ahead_of_own_creeps_pct"))
        row("distance to nearest enemy creep (median)", Lg("dist_to_nearest_enemy_creep_median"))
        row("seen by enemy % (approx.)", Lg("seen_by_enemy_pct"))

    if any(r.get("rotations") for r in mine) and any(p.get("rotations") for p in peers):
        RU = next(r["rotations"]["until_s"] for r in mine if r.get("rotations"))
        w(f"\n## Early rotations (0:30–{fmt_t(RU)})")
        w(f"A rotation = ≥ 4 s in a lane other than your own (own lane = where you spent most of 1:00–4:00). "
          f"Kill involvement = you damaged/debuffed the victim in the {12} s before it died, or got the kill.")
        header()
        R = lambda key: (lambda r: (r.get("rotations") or {}).get(key))  # noqa: E731
        row("rotations", R("count"))
        row("first rotation: left lane at (s)", R("first_s"))
        row("rotations that produced a kill you were in %", R("with_kill_pct"))
        row("rotations where you died %", R("died_pct"))
        row("rotations using TP / blink %", R("tp_pct"))
        row("kills from rotations", R("kills_from_rotations"))
        row("alive time out of your lane, 2:00 on %", R("away_from_lane_pct"))
        row("enemy hero deaths in this window", R("enemy_deaths"))
        row("…of which you were involved %", R("involved_pct"))
        row("enemy deaths in OTHER lanes", R("other_lane_kills"))
        row("…of which you were involved", R("other_lane_involved"))
        def trip_s(r_):
            return (f"{fmt_t(r_['left'])}→{r_['to']}{' (TP)' if r_['tp'] else ''}"
                    f"{' kill: ' + '+'.join(r_['kills']) if r_['kills'] else ''}{' DIED' if r_['died'] else ''}")
        w("")
        for r in mine:
            ro = r.get("rotations")
            if ro:
                w(f"- you {r['match_id']} (own lane {ro['home_lane']}): " + ("; ".join(trip_s(x) for x in ro["list"]) or "none"))
        for p in [p for p in peers if p.get("rotations") and p["win"]][:6]:
            ro = p["rotations"]
            w(f"- peer win {p['match_id']} (own lane {ro['home_lane']}): " + ("; ".join(trip_s(x) for x in ro["list"]) or "none"))

    w("\n## Movement by phase (share of alive time; Radiant-perspective zones)")
    for ph in [n for n in ("0-5m", "5-10m", "10-15m", "15-20m", "20m+")
               if any(x["phase"] == n for r in mine for x in r["phases"])]:
        def P(r, ph=ph):
            return next((x for x in r["phases"] if x["phase"] == ph), None)
        n_me = sum(1 for r in mine if P(r))
        n_p = sum(1 for p in peers if P(p))
        if not n_p:
            continue
        w(f"\n### {ph}  (your games with data: {n_me}, peers: {n_p})")
        header()
        g = lambda key: (lambda r: P(r)[key] if P(r) else None)  # noqa: E731
        row("dead % of time", g("dead_pct"))
        for z in ZONES:
            row(f"in {z} %", lambda r, z=z: P(r)["zones_pct"].get(z, 0) if P(r) else None)
        row("on enemy half %", g("enemy_half_pct"))
        row("on enemy half, no ally near %", g("enemy_half_alone_pct"))
        row("avg allies within 1500", g("avg_allies_near"))
        row("units moved / alive sec", g("move_speed_eff"))
        row("avg unspent gold", g("avg_unspent_gold"))
        row("seen by enemy % (approx.)", g("seen_pct"))
        row("seen while on enemy half %", g("seen_on_enemy_half_pct"))

    w("\n## Where last hits came from (%)")
    header("zone")
    for z in ZONES:
        row(z, lambda r, z=z: r["lh_by_zone_pct"].get(z, 0))

    w("\n## Items (≥1000 gold, first purchase time)")
    w("| item | you (median) | your games with it | peers (median) | peers with it |")
    w("|---|---|---|---|---|")
    def times(rs):
        d = {}
        for r in rs:
            for i in r["items"]:
                d.setdefault(i["item"], []).append(i["t"])
        return d
    mt, pt = times(mine), times(peers)
    keep = set(mt) | {k for k, v in pt.items() if len(v) >= max(2, len(peers) // 3)}
    for it in sorted(keep, key=lambda k: med(pt.get(k, [])) or med(mt.get(k, [])) or 9e9):
        w(f"| {it} | {fmt_t(med(mt.get(it, [])))} | {len(mt.get(it, []))}/{len(mine)} | "
          f"{fmt_t(med(pt.get(it, [])))} | {len(pt.get(it, []))}/{len(peers)} |")

    w("\n## Item build order (major items: completed, ≥2000 gold, blink/shard/BKB/aghs)")
    for r in mine:
        w(f"- you {r['match_id']}: " + " → ".join(f"{i['item']} {fmt_t(i['t'])}" for i in r.get("build", [])[:8]))
    w("")
    w("| item | your slot (median) | your time | your games | peers' slot (median) | peers' time | peers with it |")
    w("|---|---|---|---|---|---|---|")
    def slots(rs):
        d = {}
        for r in rs:
            for k_, i in enumerate(r.get("build", [])):
                d.setdefault(i["item"], []).append((k_ + 1, i["t"]))
        return d
    ms, ps = slots(mine), slots(peers)
    keep = set(ms) | {k_ for k_, v in ps.items() if len(v) >= max(2, len(peers) // 4)}
    for it in sorted(keep, key=lambda k_: med([x[0] for x in ps.get(k_, [])]) or med([x[0] for x in ms.get(k_, [])]) or 99):
        mv, pv = ms.get(it, []), ps.get(it, [])
        w(f"| {it} | {fmt(med([x[0] for x in mv])) if mv else '-'} | {fmt_t(med([x[1] for x in mv]))} | {len(mv)}/{len(mine)} | "
          f"{fmt(med([x[0] for x in pv])) if pv else '-'} | {fmt_t(med([x[1] for x in pv]))} | {len(pv)}/{len(peers)} |")

    w("\n## Skills")
    def short_skills(r):
        prefix = os.path.commonprefix([x for x in r.get("skills", []) if not x.startswith("special_bonus")])
        prefix = prefix[: prefix.rfind("_") + 1] if "_" in prefix else ""
        return [x.replace(prefix, "").replace("special_bonus_", "T:") for x in r.get("skills", [])]
    for r in mine:
        w(f"- you {r['match_id']}: {', '.join(short_skills(r)[:10])}  (maxed: {', '.join(r.get('max_order', []))})")
    counts = {}
    for p in peers:
        b_ = ", ".join(short_skills(p)[:10])
        counts[b_] = counts.get(b_, 0) + 1
    for b_, c in sorted(counts.items(), key=lambda x: -x[1])[:4]:
        w(f"- peers ×{c}: {b_}")
    mo = {}
    for p in peers:
        mo[" > ".join(p.get("max_order", [])[:2])] = mo.get(" > ".join(p.get("max_order", [])[:2]), 0) + 1
    w("- peers' max order (first two): " + "; ".join(f"{k_} ×{v}" for k_, v in sorted(mo.items(), key=lambda x: -x[1])))
    w("\n| talent | you picked it | peers who picked it |")
    w("|---|---|---|")
    tm = {}
    for r in mine:
        for t_ in r.get("talents", []):
            tm[t_] = tm.get(t_, 0) + 1
    tp = {}
    for p in peers:
        for t_ in p.get("talents", []):
            tp[t_] = tp.get(t_, 0) + 1
    AB = odota.constants("abilities")
    def tname(t_):
        import re
        d_ = (AB.get(t_) or {}).get("dname") or t_.replace("special_bonus_", "")
        return re.sub(r"\s+", " ", re.sub(r"^\+[%s]?", "+", re.sub(r"\{s:[^}]*\}", "", d_))).strip()
    for t_ in sorted(set(tm) | set(tp), key=lambda x: -tp.get(x, 0)):
        w(f"| {tname(t_)} | {tm.get(t_, 0)}/{len(mine)} | {tp.get(t_, 0)}/{len(peers)} |")

    if all(r.get("fights") for r in mine) and any(p.get("fights") for p in peers):
        F = lambda key: (lambda r: (r.get("fights") or {}).get(key))  # noqa: E731
        w("\n## Teamfights")
        w("Fights = clusters of hero deaths; onset = first hero-vs-hero action in the chain leading to the first "
          "death. 'Present' = you dealt/took hero damage or were within 1500 units.")
        header()
        row("fights in game", F("n"))
        row("present %", F("present_pct"))
        row("missed while alive (count)", F("absent_alive"))
        row("missed while alive: distance at onset", F("absent_alive_median_dist"))
        row("engage delay after onset, s (median)", F("median_engage_delay"))
        row(f"joined late (>5 s) %", F("late_pct"))
        row("first on your team to engage %", F("first_on_team_pct"))
        row("opened the fight (first hostile action) %", F("opener_pct"))
        row("your team engaged first %", F("team_engaged_first_pct"))
        row("died in fight %", F("died_pct"))
        row("died first on your team %", F("died_first_pct"))
        row("fights won (more enemy deaths) %", F("won_pct"))
        row("hero damage per fight", F("dmg_per_fight"))
        row("share of team's fight damage %", F("dmg_team_share_pct"))
        row("seen by enemy in the 10 s before onset % (approx.)", F("seen_10s_before_onset_pct"))
        row("depth: 0 = front-most of your team, 1 = back-most", F("depth_median"))
        row("distance to nearest enemy (median)", F("nearest_enemy_median"))
        row("moving toward enemies % of fight time", F("moving_in_pct"))
        row("moving away from enemies %", F("moving_out_pct"))
        row("below 30% HP: moving toward enemies %", F("low_hp_moving_in_pct"))
        row("below 30% HP: moving away %", F("low_hp_moving_out_pct"))
        row("gap between 1st and 2nd spell, s", F("combo_gap_s"))
        w("\n**Opening sequences** (first two spells/items you used in a fight, % of fights)")
        def openers(rs):
            c = {}
            for r in rs:
                for f_ in (r.get("fights") or {}).get("list", []):
                    if f_.get("present") and f_.get("seq"):
                        k_ = " > ".join(x.replace("item_", "").split("_", 1)[-1] if not x.startswith("item_") else x[5:]
                                        for x in f_["seq"][:2])
                        c[k_] = c.get(k_, 0) + 1
            n_ = sum(c.values()) or 1
            return ", ".join(f"{k_} {round(100 * v / n_)}%" for k_, v in sorted(c.items(), key=lambda x: -x[1])[:4])
        w(f"- you: {openers(mine)}")
        w(f"- peers: {openers(peers)}")
        w(f"- peers who won: {openers([p for p in peers if p['win']])}")
        w("\n**Spells left unused** (ready — learned, off cooldown, enough mana — at the end of the fight or at "
          "your death, and not cast during it; % of fights you were in. Items: only games where you had the item)")
        w("| spell/item | you: fights ended unused | peers | you: died with it ready | peers |")
        w("|---|---|---|---|---|")
        U = lambda r, k_: ((r.get("fights") or {}).get(k_) or {})  # noqa: E731
        keys_ = {x for r in mine for x in U(r, "unused_pct")} | {x for p in peers for x in U(p, "unused_pct")
                                                                if sum(1 for q in peers if x in U(q, "unused_pct")) >= 3}
        def has(r, x):  # items: only count games where the player had it (used it, or it was ever ready)
            return not x.startswith("item_") or x in U(r, "abilities") or x in U(r, "unused_pct") \
                or x in U(r, "died_with_ready_pct")
        def um(rs, k_, x):
            vals = [U(r, k_).get(x, 0) for r in rs if r.get("fights") and has(r, x)]
            return f"{fmt(med(vals))}%" if vals else "-"
        for x in sorted(keys_, key=lambda x: -(med([U(r, "unused_pct").get(x, 0) for r in mine]) or 0)):
            cells = [um(rs_, k_, x) for k_ in ("unused_pct", "died_with_ready_pct") for rs_ in (mine, peers)]
            if all(c_ in ("0%", "-") for c_ in cells):
                continue
            w(f"| {x} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} |")
        w("\n**Who you hit** (share of your fight damage by enemy net-worth rank at fight start; 1 = richest enemy)")
        header("target")
        for rk in "12345":
            row(f"enemy #{rk} by NW", lambda r, rk=rk: ((r.get("fights") or {}).get("dmg_by_enemy_nw_rank_pct") or {}).get(rk, ((r.get("fights") or {}).get("dmg_by_enemy_nw_rank_pct") or {}).get(int(rk), 0)))
        w("\n**Spells and items in fights** (per fight you were present in; casts/hits/timing are medians over games where it was used)")
        w("| spell/item | you: used in % fights | peers | you: casts/fight | peers | you: enemies hit/cast | peers | "
          "you: 1st use, s after onset | peers |")
        w("|---|---|---|---|---|---|---|---|---|")
        A = lambda r: (r.get("fights") or {}).get("abilities") or {}  # noqa: E731
        names = {k_ for r in mine for k_ in A(r)} | {k_ for p in peers for k_ in A(p)
                                                     if sum(1 for q in peers if k_ in A(q)) >= max(2, len(peers) // 4)}
        def am(rs, k_, f_):
            return med([A(r)[k_][f_] for r in rs if k_ in A(r)])
        for k_ in sorted(names, key=lambda x: -(am(peers, x, "used_in_pct_of_fights") or 0)):
            vals = []
            for f_ in ("used_in_pct_of_fights", "casts_per_fight", "avg_enemies_hit", "median_first_use_s"):
                a_ = med([A(r).get(k_, {}).get(f_, 0 if f_ == "used_in_pct_of_fights" else None) for r in mine])
                b_ = med([A(p).get(k_, {}).get(f_, 0 if f_ == "used_in_pct_of_fights" else None) for p in peers])
                vals += [fmt(a_) if a_ is not None else "-", fmt(b_) if b_ is not None else "-"]
            w(f"| {k_} | " + " | ".join(vals) + " |")
        w("\n**Your fights**")
        w("| match | death time | onset | present | engage delay | first on team | opener | died | won | top target | depth | in/out % | seen before | spells (s after onset, enemies hit) | ready but unused |")
        w("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for r in mine:
            for f_ in r["fights"]["list"]:
                if not f_["present"]:
                    w(f"| {r['match_id']} | {fmt_t(f_['t'])} | {fmt_t(f_['onset'])} | no ({f_.get('absent')}"
                      f"{', ' + str(f_['dist_at_onset']) + ' away' if f_.get('dist_at_onset') else ''}) | | | | | | | | | | | |")
                    continue
                sp = ", ".join(f"{c['what'].replace('item_', '')} {c['t']:+g}s" + (f" ({c['hits']})" if c['hits'] else "")
                               for c in f_["casts"])
                mv_ = f_.get("move") or {}
                tot_mv = sum(mv_.values())
                io = f"{round(100 * mv_.get('in', 0) / tot_mv)}/{round(100 * mv_.get('out', 0) / tot_mv)}" if tot_mv else "-"
                sb = f"{round(100 * f_['seen_before'])}%" if f_.get("seen_before") is not None else "-"
                un = ", ".join(x.replace("item_", "") for x in f_.get("unused") or [])
                w(f"| {r['match_id']} | {fmt_t(f_['t'])} | {fmt_t(f_['onset'])} | yes | {f_['engage_delay']} | "
                  f"{'✓' if f_['first_on_team'] else ''} | {'✓' if f_['opener'] else ''} | {'✗' if f_['died'] else ''} | "
                  f"{'W' if f_['won'] else 'L'} {f_['deaths_enemy']}-{f_['deaths_ally']} | {f_['top_target'] or '-'} | {f_.get('depth', '-')} | {io} | {sb} | {sp} | {un} |")

    w("\n## Your deaths")
    w("| match | time | zone | allies near | enemies near | killer | seen by enemy, s of last 15 |")
    w("|---|---|---|---|---|---|---|")
    for r in mine:
        for d in r["deaths"]:
            w(f"| {r['match_id']} | {fmt_t(d['t'])} | {d['zone']} | {d['allies_near']} | {d['enemies_near']} | "
              f"{d['killer']} | {d.get('seen_s_of_15', '-')} |")
    row_d = lambda rs: med([d.get("seen_s_of_15") for r in rs for d in r["deaths"]])  # noqa: E731
    w(f"\nMedian seconds seen by the enemy in the 15 s before dying: you {row_d(mine)}, peers {row_d(peers)}.")
    pdeaths = [d for p in peers for d in p["deaths"]]
    pmin = sum(p["duration_min"] for p in peers)
    mdeaths = [d for r in mine for d in r["deaths"]]
    mmin = sum(r["duration_min"] for r in mine)
    if pdeaths:
        w("\n| death zone | your deaths / 10 min | peer deaths / 10 min |")
        w("|---|---|---|")
        for z in ZONES:
            a_, b_ = sum(1 for d in mdeaths if d["zone"] == z), sum(1 for d in pdeaths if d["zone"] == z)
            if a_ or b_:
                w(f"| {z} | {10 * a_ / mmin:.2f} | {10 * b_ / pmin:.2f} |")

    w(f"\n## Heatmap\n`{png}` — rows: 0-10 min, 10-20 min, 20+ min; left = you{' (all games pooled)' if multi else ''}, "
      "right = peers pooled. Radiant perspective (your base bottom-left), red X = deaths, blue line = river, "
      "grey = lanes/mid.")
    tl_dir = os.path.join(out_dir, f"{tag}_timelines")
    os.makedirs(tl_dir, exist_ok=True)
    for old in os.listdir(tl_dir):
        os.remove(os.path.join(tl_dir, old))
    for who, rs in (("you", mine), ("peer", peers)):
        for r in rs:
            if r.get("timeline"):
                with open(os.path.join(tl_dir, f"{who}_{r['match_id']}_{'W' if r['win'] else 'L'}.txt"), "w") as f:
                    f.write(r["timeline"])
    w(f"\n## Timelines (for a holistic read)\nMinute-by-minute digests of every game above: `{tl_dir}/` "
      f"(`you_*` = your games, `peer_*_W` = peer wins). See SKILL.md → *Holistic read*.")
    st = odota.STATS
    retried = f" ({st['rate_limited']} were rate-limited and retried)" if st["rate_limited"] else ""
    w(f"\n## Cost\nOpenDota API requests this run: **{st['requests']}**{retried}; "
      f"{'API key' if odota.API_KEY else 'free tier, no key'}. Cached matches, searches and replays cost nothing. "
      "Replay downloads come from Valve, not OpenDota.")
    L.insert(1, f"\n_OpenDota requests this run: {st['requests']}{retried}._")
    report = "\n".join(L)
    md_path = os.path.join(out_dir, f"{tag}.md")
    with open(md_path, "w") as f:
        f.write(report)
    with open(os.path.join(out_dir, f"{tag}.json"), "w") as f:
        json.dump({"me": [{k: v for k, v in r.items() if k not in ("path", "timeline")} for r in mine],
                   "peers": [{k: v for k, v in p.items() if k not in ("path", "timeline")} for p in peers]}, f)
    print(report)
    log(f"\nsaved {md_path} and {png} ({odota.STATS['requests']} OpenDota requests)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("games")
    g.add_argument("--account", type=int, default=DEFAULT_ACCOUNT)
    g.add_argument("--n", type=int, default=20)
    for name in ("peers", "analyze", "compare", "timeline"):
        s = sub.add_parser(name)
        if name == "compare":
            s.add_argument("match", type=int, nargs="+", help="one or more of your match ids (same hero)")
        else:
            s.add_argument("match", type=int)
        s.add_argument("--account", type=int, default=DEFAULT_ACCOUNT)
        s.add_argument("--hero", type=int)
        s.add_argument("--k", type=int, default=6)
        s.add_argument("--days", type=int, default=12)
        s.add_argument("--min-rank", type=int, default=72)
        s.add_argument("--role", choices=["auto", "any", "core", "support"], default="auto",
                       help="peer role filter; auto = whatever you played in each game (team GPM rank)")
        s.add_argument("--lane", choices=["auto", "any", "safe", "mid", "off", "jungle"], default="auto",
                       help="peer early-lane filter (needs the parsed replay); auto = your lane in each game")
        s.add_argument("--peers", help="comma-separated peer match ids (skip the search)")
    a = ap.parse_args()
    if a.account is None and not getattr(a, "hero", None):
        ap.error("no account: pass --account <OpenDota account id> or set DOTA_COACH_ACCOUNT")
    {"games": cmd_games, "peers": cmd_peers, "analyze": cmd_analyze, "compare": cmd_compare,
     "timeline": cmd_timeline}[a.cmd](a)


if __name__ == "__main__":
    main()
