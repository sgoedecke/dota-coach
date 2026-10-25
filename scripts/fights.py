"""Teamfight analysis from replay combat-log events + per-second positions.

A "fight" is a cluster of hero deaths (gaps <= 18s, within 4000 units). Its onset is found by walking back
from the first death through the chain of hero-vs-hero hostile events (damage, debuffs, targeted casts)
near the fight, stopping at the first gap > 6s.
"""
import math
import statistics
from collections import Counter

import odota

GAP = 18
CHAIN_GAP = 6
NEAR_FIGHT = 2500
LATE = 5  # seconds after onset counts as "joined late"
NOISE = {"item_phase_boots", "item_tpscroll", "item_travel_boots", "item_travel_boots_2", "item_magic_stick",
         "item_magic_wand", "item_faerie_fire", "item_flask", "item_tango", "item_clarity", "item_power_treads",
         "ability_lamp_use", "item_ward_observer", "item_ward_sentry", "item_ward_dispenser", "item_bottle",
         "item_enchanted_mango", "item_quelling_blade", "item_bfury", "item_dust", "ability_pluck_famango", "item_arcane_boots", "item_famango", "item_great_famango",
         "item_greater_famango", "twin_gate_portal_warp", "item_jidi_pollen_bag", "seasonal_dark_carnival_firework"}


def _npc(rep, p):
    want = p["hero_class"].replace("CDOTA_Unit_Hero_", "").replace("_", "").lower()
    for e in rep.get("events") or []:
        if e[1] in ("cast", "buy", "item") and e[2].replace("_", "").lower() == want:
            return e[2]
    return want


_ACTIVE = {}


def is_active(name):
    """True for abilities/items that are cast (not passives)."""
    if name not in _ACTIVE:
        if name.startswith("item_"):
            it = odota.constants("items").get(name[5:]) or {}
            types = {a.get("type") for a in it.get("abilities") or []}
            _ACTIVE[name] = bool(it.get("cd")) and bool(types & {"active", "use"})
        else:
            ab = odota.constants("abilities").get(name) or {}
            b = ab.get("behavior") or ""
            b = " ".join(b) if isinstance(b, list) else str(b)
            _ACTIVE[name] = bool(ab) and "Passive" not in b and "Hidden" not in b
    return _ACTIVE[name]


def ready_list(state):
    """Active abilities/items that could be cast right now (learned, off cooldown / has charges, enough mana)."""
    out = []
    for name, lvl, cd, mc, charges in state["abil"]:
        if lvl <= 0 or name in NOISE or not is_active(name):
            continue
        ok = charges > 0 if charges >= 0 else cd <= 0.05
        if ok and state["mana"] >= mc:
            out.append(name)
    return out


def _med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def fight_analysis(rep, me):
    ev = rep.get("events")
    if not ev:
        return None
    players = rep["players"]
    npc = {p["idx"]: _npc(rep, p) for p in players}
    team_of = {npc[p["idx"]]: p["team"] for p in players}
    by_npc = {npc[p["idx"]]: p for p in players}
    tr = {int(k): v for k, v in rep["track"].items()}
    t0 = tr[me["idx"]]["t"][0]
    my = npc[me["idx"]]
    my_team = me["team"]
    cls_npc = {p["hero_class"]: npc[p["idx"]] for p in players}
    states = rep.get("states") or []

    def qpos(name, t):
        p = by_npc.get(name)
        if p is None:
            return None
        T = tr[p["idx"]]
        Q = T.get("q")
        if not Q:
            return None
        i = int(round(t * 4)) - Q[0]
        if 0 <= i < len(Q) and T["qhp"][i] >= 0:
            return T["qx"][i], T["qy"][i], T["qhp"][i]
        return None

    def seen_frac(t_from, t_to):
        T = tr[me["idx"]]
        V = T.get("vis")
        if not V:
            return None
        xs = [V[s - t0] for s in range(int(t_from), int(t_to)) if 0 <= s - t0 < len(V) and T["alive"][s - t0]]
        return round(sum(xs) / len(xs), 2) if xs else None

    def my_state(t, reason=None):
        best = None
        for st in states:
            if abs(st["t"] - t) <= 0.6 and (reason is None or st["reason"] == reason):
                best = st
        if not best:
            return None
        for h in best["heroes"]:
            if cls_npc.get(h["hero"]) == my:
                return h
        return None

    def pos(name, t):
        p = by_npc.get(name)
        if p is None:
            return None
        T = tr[p["idx"]]
        i = int(t) - t0
        if 0 <= i < len(T["t"]) and T["alive"][i]:
            return T["x"][i], T["y"][i]
        return None

    def nw_at(name, t):
        T = tr[by_npc[name]["idx"]]
        i = min(max(0, int(t) - t0), len(T["t"]) - 1)
        return T["nw"][i]

    hostile = [e for e in ev if e[1] in ("dmg", "debuff", "cast") and e[2] in team_of and e[3] in team_of
               and team_of[e[2]] != team_of[e[3]]]
    deaths = []
    for d in rep["deaths"]:
        v = d["victim"].replace("npc_dota_hero_", "")
        if v in team_of:
            deaths.append({"t": d["t"], "v": v, "x": d["x"], "y": d["y"]})
    deaths.sort(key=lambda d: d["t"])
    groups = []
    for d in deaths:
        g = groups[-1] if groups else None
        if g and d["t"] - g[-1]["t"] <= GAP and math.hypot(d["x"] - g[0]["x"], d["y"] - g[0]["y"]) < 4000:
            g.append(d)
        else:
            groups.append([d])

    fights = []
    for g in groups:
        t_first, t_last = g[0]["t"], g[-1]["t"]
        cx = sum(d["x"] for d in g) / len(g)
        cy = sum(d["y"] for d in g) / len(g)

        def near(name, t, r=3000):
            q = pos(name, t)
            return q is not None and math.hypot(q[0] - cx, q[1] - cy) < r

        E = [e for e in hostile if t_first - 30 <= e[0] <= t_last + 3 and (near(e[2], e[0]) or near(e[3], e[0]))]
        E.sort(key=lambda e: e[0])
        pre = [e for e in E if e[0] <= t_first]
        if pre:
            k = len(pre) - 1
            while k > 0 and pre[k][0] - pre[k - 1][0] <= CHAIN_GAP:
                k -= 1
            onset = pre[k][0]
        else:
            onset = t_first - 3
        end = t_last + 3
        E = [e for e in E if onset <= e[0] <= end]
        first = {}
        for e in E:
            for who in (e[2], e[3]):
                first.setdefault(who, e[0])
        first_src = {}
        for e in E:
            first_src.setdefault(e[2], e[0])
        my_deaths = [d for d in g if d["v"] == my]
        ally_deaths = sum(1 for d in g if team_of[d["v"]] == my_team)
        enemy_deaths = len(g) - ally_deaths
        my_alive_at_onset = pos(my, onset) is not None
        present = my in first or any(near(my, t, 1500) for t in range(int(onset), int(end) + 1))
        f = {"t": t_first, "onset": round(onset, 1), "deaths_ally": ally_deaths, "deaths_enemy": enemy_deaths,
             "present": present, "zone_xy": [round(cx), round(cy)]}
        if not present:
            q = pos(my, onset)
            f["absent"] = "dead" if not my_alive_at_onset and not q else "alive"
            if q:
                f["dist_at_onset"] = round(math.hypot(q[0] - cx, q[1] - cy))
            fights.append(f)
            continue
        team_first = sorted((first_src[n], n) for n in first_src if team_of[n] == my_team)
        enemy_first = min((first_src[n] for n in first_src if team_of[n] != my_team), default=None)
        f["engage_delay"] = round(first[my] - onset, 1) if my in first else None
        f["my_first_action"] = round(first_src[my] - onset, 1) if my in first_src else None
        f["first_on_team"] = bool(team_first) and team_first[0][1] == my
        f["team_engaged_first"] = bool(team_first) and (enemy_first is None or team_first[0][0] <= enemy_first)
        f["opener"] = bool(E) and E[0][2] == my
        f["died"] = bool(my_deaths)
        f["died_first_on_team"] = bool(my_deaths) and next(d for d in g if team_of[d["v"]] == my_team)["v"] == my
        f["won"] = enemy_deaths > ally_deaths
        # my damage by enemy net-worth rank at onset (1 = richest enemy)
        enemies = [n for n in team_of if team_of[n] != my_team]
        rank = {n: i + 1 for i, n in enumerate(sorted(enemies, key=lambda n: -nw_at(n, onset)))}
        dmg = {}
        for e in E:
            if e[1] == "dmg" and e[2] == my:
                dmg[e[3]] = dmg.get(e[3], 0) + e[5]
        team_dmg = sum(e[5] for e in E if e[1] == "dmg" and team_of[e[2]] == my_team)
        f["dmg"] = sum(dmg.values())
        f["dmg_team_share"] = round(f["dmg"] / team_dmg, 2) if team_dmg else None
        f["dmg_by_rank"] = {str(rank[n]): v for n, v in dmg.items()}
        f["top_target"] = max(dmg, key=dmg.get) if dmg else None
        # casts during the fight (abilities + items), and how many enemy heroes each one touched
        casts = [e for e in ev if e[1] in ("cast", "item") and e[2] == my and onset - 3 <= e[0] <= end
                 and e[4] not in NOISE]
        cl = []
        for c in casts:
            key = c[4].replace("item_", "")
            hit = {e[3] for e in hostile if e[2] == my and c[0] <= e[0] <= c[0] + 2 and e[1] in ("dmg", "debuff")
                   and key and key in e[4]}
            cl.append({"t": round(c[0] - onset, 1), "what": c[4], "hits": len(hit)})
        f["casts"] = cl
        f["seq"] = [c["what"] for c in cl if c["t"] >= -1]
        f["combo_gap"] = round(cl[1]["t"] - cl[0]["t"], 1) if len(cl) >= 2 else None
        # positioning & movement at 4 Hz
        depth, near_d, mv, low = [], [], Counter(), Counter()
        allies = [n for n in team_of if team_of[n] == my_team]
        foes = [n for n in team_of if team_of[n] != my_team]
        q = onset
        while q <= end:
            me_q = qpos(my, q)
            if me_q:
                E_ = [qpos(n, q) for n in foes]
                E_ = [e for e in E_ if e and math.hypot(e[0] - cx, e[1] - cy) < 3000]
                if E_:
                    ex = sum(e[0] for e in E_) / len(E_)
                    ey = sum(e[1] for e in E_) / len(E_)
                    A_ = [qpos(n, q) for n in allies if n != my]
                    A_ = [a for a in A_ if a and math.hypot(a[0] - cx, a[1] - cy) < 3000]
                    mine_d = math.hypot(me_q[0] - ex, me_q[1] - ey)
                    if A_:  # share of nearby allies closer to the enemy than me: 0 = front-most, 1 = back-most
                        closer = sum(1 for a in A_ if math.hypot(a[0] - ex, a[1] - ey) < mine_d)
                        depth.append(closer / len(A_))
                    near_d.append(min(math.hypot(me_q[0] - e[0], me_q[1] - e[1]) for e in E_))
                    nxt = qpos(my, q + 0.5)
                    if nxt and mine_d > 1:
                        vx, vy = (nxt[0] - me_q[0]) / 0.5, (nxt[1] - me_q[1]) / 0.5
                        if math.hypot(vx, vy) < 1500:  # ignore blinks/teleports
                            toward = (vx * (ex - me_q[0]) + vy * (ey - me_q[1])) / mine_d
                            st = "in" if toward > 120 else "out" if toward < -120 else "hold"
                            mv[st] += 1
                            if me_q[2] < 30:
                                low[st] += 1
            q += 0.25
        f["depth"] = _med(depth)
        f["nearest_enemy"] = _med(near_d)
        f["move"] = dict(mv)
        f["low_hp_move"] = dict(low)
        f["seen_before"] = seen_frac(onset - 10, onset)
        # spells left unused: ready at the end of the fight (or at my death) and never cast in it
        cast_names = {c["what"] for c in cl}
        st = my_state(g[-1]["t"])
        if my_deaths:
            st = my_state(my_deaths[0]["t"], "death:" + my) or st
        if st:
            f["unused"] = [n for n in ready_list(st) if n not in cast_names]
        fights.append(f)

    pres = [f for f in fights if f["present"]]
    n = len(fights)
    abil = {}
    for f in pres:
        used = set()
        for c in f["casts"]:
            a = abil.setdefault(c["what"], {"casts": 0, "fights": 0, "hits": [], "t": []})
            a["casts"] += 1
            a["hits"].append(c["hits"])
            if c["what"] not in used:
                used.add(c["what"])
                a["fights"] += 1
                a["t"].append(c["t"])
    abilities = {k: {"casts_per_fight": round(v["casts"] / len(pres), 2),
                     "used_in_pct_of_fights": round(100 * v["fights"] / len(pres)),
                     "avg_enemies_hit": round(sum(v["hits"]) / len(v["hits"]), 2),
                     "median_first_use_s": _med(v["t"])}
                 for k, v in abil.items()} if pres else {}
    seqs = Counter(" > ".join(x.replace("item_", "") for x in f["seq"][:2]) for f in pres if f.get("seq"))
    mv, low = Counter(), Counter()
    for f in pres:
        mv.update(f.get("move") or {})
        low.update(f.get("low_hp_move") or {})
    with_state = [f for f in pres if "unused" in f]
    unused = Counter(n for f in with_state for n in f["unused"])
    died_states = [f for f in with_state if f["died"]]
    died_unused = Counter(n for f in died_states for n in f["unused"])
    my_deaths_all = [d for d in deaths if d["v"] == my]
    tot = {}
    for f in pres:
        for r, v in f["dmg_by_rank"].items():
            tot[r] = tot.get(r, 0) + v
    s = sum(tot.values())

    def pct(xs):
        xs = list(xs)
        return round(100 * sum(1 for x in xs if x) / len(xs)) if xs else None

    return {
        "n": n,
        "present": len(pres),
        "present_pct": pct(f["present"] for f in fights),
        "absent_alive": sum(1 for f in fights if f.get("absent") == "alive"),
        "absent_dead": sum(1 for f in fights if f.get("absent") == "dead"),
        "absent_alive_median_dist": _med([f.get("dist_at_onset") for f in fights if f.get("absent") == "alive"]),
        "median_engage_delay": _med([f["engage_delay"] for f in pres]),
        "late_pct": pct(f["engage_delay"] is None or f["engage_delay"] > LATE for f in pres),
        "first_on_team_pct": pct(f["first_on_team"] for f in pres),
        "team_engaged_first_pct": pct(f["team_engaged_first"] for f in pres),
        "opener_pct": pct(f["opener"] for f in pres),
        "died_pct": pct(f["died"] for f in pres),
        "died_first_pct": pct(f["died_first_on_team"] for f in pres),
        "won_pct": pct(f["won"] for f in pres),
        "dmg_per_fight": round(sum(f["dmg"] for f in pres) / len(pres)) if pres else None,
        "dmg_team_share_pct": round(100 * _med([f["dmg_team_share"] for f in pres]))
        if _med([f["dmg_team_share"] for f in pres]) is not None else None,
        "dmg_by_enemy_nw_rank_pct": {r: round(100 * v / s) for r, v in sorted(tot.items())} if s else {},
        "abilities": abilities,
        "openers": {k: round(100 * v / len(pres)) for k, v in seqs.most_common(4)} if pres else {},
        "combo_gap_s": _med([f.get("combo_gap") for f in pres]),
        "depth_median": _med([f.get("depth") for f in pres]),
        "nearest_enemy_median": _med([f.get("nearest_enemy") for f in pres]),
        "moving_in_pct": round(100 * mv["in"] / sum(mv.values())) if mv else None,
        "moving_out_pct": round(100 * mv["out"] / sum(mv.values())) if mv else None,
        "low_hp_moving_in_pct": round(100 * low["in"] / sum(low.values())) if sum(low.values()) >= 4 else None,
        "low_hp_moving_out_pct": round(100 * low["out"] / sum(low.values())) if sum(low.values()) >= 4 else None,
        "seen_10s_before_onset_pct": round(100 * _med([f.get("seen_before") for f in pres]))
        if _med([f.get("seen_before") for f in pres]) is not None else None,
        "unused_pct": {k: round(100 * v / len(with_state)) for k, v in unused.items()} if with_state else {},
        "died_with_ready_pct": {k: round(100 * v / len(died_states)) for k, v in died_unused.items()}
        if died_states else {},
        "deaths_seen_15s_before_pct": round(100 * _med([seen_frac(d["t"] - 15, d["t"]) for d in my_deaths_all]))
        if _med([seen_frac(d["t"] - 15, d["t"]) for d in my_deaths_all]) is not None else None,
        "list": fights,
    }
