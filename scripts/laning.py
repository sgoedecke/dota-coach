"""Laning-phase analysis from replay data (parser v3: lane creeps, creep deaths, 4 Hz track, combat log).

All positions are raw world coordinates; "progress" along a lane is approximated by the distance from the
player's own fountain, which increases monotonically along all three lanes.
"""
import math
import statistics

LANE_END = 420  # seconds; Turbo laning is short
FOUNTAIN = {2: (-7000, -6500), 3: (7000, 6500)}
NEAR = 600  # creep deaths within this distance count as "in your last-hit range"
REGEN = {"item_tango", "item_flask", "item_clarity", "item_faerie_fire", "item_enchanted_mango", "item_bottle",
         "item_famango", "item_great_famango", "item_greater_famango", "item_tango_single", "item_magic_stick",
         "item_magic_wand", "item_blood_grenade"}


def _med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def lane_analysis(rep, me, npc, team_of, lane_end=LANE_END):
    creeps = rep.get("creeps")
    if not creeps or not rep.get("track"):
        return None
    tr = {int(k): v for k, v in rep["track"].items()}
    by_npc = {npc[p["idx"]]: p for p in rep["players"]}
    T = tr[me["idx"]]
    t0 = T["t"][0]
    my, team = npc[me["idx"]], me["team"]
    fx, fy = FOUNTAIN[team]

    def at(p, s):
        A = tr[p["idx"]]
        i = s - A["t"][0]
        if 0 <= i < len(A["t"]) and A["alive"][i]:
            return A["x"][i], A["y"][i]
        return None

    secs = [s for s in T["t"] if 0 <= s < lane_end]
    # lane opponents / partners: heroes within 1500 of me for >= 40% of 1:00-6:00
    window = [s for s in secs if 60 <= s < min(360, lane_end)]
    close = {}
    for s in window:
        q = at(me, s)
        if not q:
            continue
        for n, p in by_npc.items():
            if n == my:
                continue
            r = at(p, s)
            if r and math.dist(q, r) < 1500:
                close[n] = close.get(n, 0) + 1
    alive_n = sum(1 for s in window if at(me, s)) or 1
    opponents = sorted(n for n, c in close.items() if team_of[n] != team and c / alive_n >= 0.4)
    partners = sorted(n for n, c in close.items() if team_of[n] == team and c / alive_n >= 0.4)

    # creep snapshot index by second
    snap = {}
    C = creeps
    for i, s in enumerate(C["t"]):
        if s < lane_end:
            snap.setdefault(s, []).append((C["team"][i], C["x"][i], C["y"][i]))

    def prog(x, y):
        return math.hypot(x - fx, y - fy)

    ahead, enemy_creep_d = [], []
    for s in secs:
        q = at(me, s)
        if not q or s < 30:
            continue
        mine_c = [(x, y) for tm, x, y in snap.get(s, []) if tm == team and math.dist((x, y), q) < 1800]
        theirs = [(x, y) for tm, x, y in snap.get(s, []) if tm != team and math.dist((x, y), q) < 1800]
        if not mine_c or not theirs:
            continue
        own_front = max(prog(*c) for c in mine_c)
        ahead.append(prog(*q) - own_front)
        enemy_creep_d.append(min(math.dist(q, c) for c in theirs))

    # last-hit / deny efficiency vs creeps dying near me
    D = rep.get("creep_deaths") or {"t": []}
    lh_s = {s: v for s, v in zip(T["t"], T["lh"])}
    dn_s = {s: v for s, v in zip(T["t"], T["dn"])}
    near_enemy, got_lh, near_ally, got_dn = 0, 0, 0, 0
    lh_secs = {s for s in secs if lh_s.get(s, 0) > lh_s.get(s - 1, 0)}
    dn_secs = {s for s in secs if dn_s.get(s, 0) > dn_s.get(s - 1, 0)}
    for i, t10 in enumerate(D["t"]):
        t = t10 / 10
        if not 0 <= t < lane_end:
            continue
        q = at(me, int(t))
        if not q or math.dist(q, (D["x"][i], D["y"][i])) > NEAR:
            continue
        s = int(t)
        if D["team"][i] != team:
            near_enemy += 1
            hit = next((x for x in (s, s + 1) if x in lh_secs), None)
            if hit is not None:
                got_lh += 1
                lh_secs.discard(hit)
        else:
            near_ally += 1
            hit = next((x for x in (s, s + 1) if x in dn_secs), None)
            if hit is not None:
                got_dn += 1
                dn_secs.discard(hit)

    # harass & damage taken
    ev = [e for e in rep.get("events") or [] if 0 <= e[0] < lane_end]
    mins = lane_end / 60
    dealt = sum(e[5] for e in ev if e[1] == "dmg" and e[2] == my)
    dealt_attack = sum(e[5] for e in ev if e[1] == "dmg" and e[2] == my and e[4] in ("", "dota_unknown"))
    taken_h = sum(e[5] for e in ev if e[1] == "dmg" and e[3] == my and e[2] in team_of)
    taken_c = sum(e[5] for e in ev if e[1] == "dmg" and e[3] == my and "creep" in e[2] and "neutral" not in e[2])
    taken_t = sum(e[5] for e in ev if e[1] == "dmg" and e[3] == my and "tower" in e[2])
    # my ability casts (cooldown starts, so ground-targeted spells count) and whether they touched an enemy hero
    casts = [e for e in ev if e[1] == "cd" and e[2] == me["hero_class"] and not e[4].startswith("item_")]
    hits = [e for e in ev if e[1] in ("dmg", "debuff") and e[2] == my and e[3] in team_of and team_of[e[3]] != team]
    landed = sum(1 for c in casts if any(c[0] <= h[0] <= c[0] + 3 and h[4] == c[4] for h in hits))
    regen = sum(1 for e in ev if e[1] == "item" and e[2] == my and e[4] in REGEN)

    hp = [h for q_, h in zip(T.get("q", []), T.get("qhp", [])) if 0 <= q_ / 4 < lane_end and h >= 0]
    deaths = sum(1 for d in rep["deaths"] if 0 <= d["t"] < lane_end and d["victim"].replace("npc_dota_hero_", "") == my)
    vis = [v for s, v, a in zip(T["t"], T.get("vis", []), T["alive"]) if 0 <= s < lane_end and a]

    def lh_at(sec):
        i = min(max(0, sec - t0), len(T["t"]) - 1)
        return T["lh"][i], T["dn"][i]

    return {
        "lane_end_s": lane_end,
        "opponents": opponents,
        "partners": partners,
        "lh_dn_2m": lh_at(120), "lh_dn_4m": lh_at(240), "lh_dn_end": lh_at(lane_end),
        "lh_pct_of_nearby_enemy_creeps": round(100 * got_lh / near_enemy) if near_enemy else None,
        "nearby_enemy_creep_deaths": near_enemy,
        "deny_pct_of_nearby_ally_creeps": round(100 * got_dn / near_ally) if near_ally else None,
        "hero_dmg_dealt_per_min": round(dealt / mins),
        "hero_dmg_taken_per_min": round(taken_h / mins),
        "creep_dmg_taken_per_min": round(taken_c / mins),
        "tower_dmg_taken_per_min": round(taken_t / mins),
        "attack_dmg_on_heroes_per_min": round(dealt_attack / mins),
        "spell_casts_per_min": round(len(casts) / mins, 2),
        "spell_casts_hit_hero_pct": round(100 * landed / len(casts)) if casts else None,
        "regen_uses": regen,
        "avg_hp_pct": round(sum(hp) / len(hp)) if hp else None,
        "low_hp_time_pct": round(100 * sum(1 for h in hp if h < 30) / len(hp)) if hp else None,
        "deaths": deaths,
        "ahead_of_own_creeps_median": _med(ahead),
        "ahead_of_own_creeps_pct": round(100 * sum(1 for a in ahead if a > 150) / len(ahead)) if ahead else None,
        "dist_to_nearest_enemy_creep_median": _med(enemy_creep_d),
        "seen_by_enemy_pct": round(100 * sum(vis) / len(vis)) if vis else None,
    }
