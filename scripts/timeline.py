"""Minute-by-minute text digest of one player's game, for a holistic (LLM) read of replays.

Everything here is factual, derived from the parsed replay; interpretation is left to the reader."""

RUNES = {"0": "double damage", "1": "haste", "2": "illusion", "3": "invisibility", "4": "regeneration",
         "5": "bounty", "6": "arcane", "7": "water", "8": "wisdom", "9": "shield"}
NEAR = 1500
TOGGLES = {"phase_boots", "power_treads", "ward_dispenser", "madstone_bundle", "magic_stick", "magic_wand"}
CONSUMABLE = {"tango", "flask", "clarity", "enchanted_mango", "famango", "great_famango", "faerie_fire", "branches",
              "blood_grenade", "ward_observer", "ward_sentry", "ward_dispenser", "smoke_of_deceit", "dust",
              "tpscroll", "tome_of_knowledge", "mana_draught", "bottle"}


def _n(s):
    return s.replace("CDOTA_Unit_Hero_", "").replace("npc_dota_hero_", "").replace("_", "").lower()


def _t(s):
    s = int(round(s))
    return f"{'-' if s < 0 else ''}{abs(s) // 60}:{abs(s) % 60:02d}"


def timeline(rep, me, team, rec, zone, norm_xy, costs, hero_names, npc):
    """rec = the analyze() result (fights, rotations, deaths, ...). costs: item -> gold.
    hero_names: hero_id -> readable name. npc: the player's combat-log name ("primal_beast")."""
    readable = {_n(p["hero_class"]): hero_names.get(p["hero_id"], p["hero_class"]) for p in rep["players"]}

    def display(x):
        return readable.get(_n(x or ""), (x or "?").replace("npc_dota_", ""))

    def short(ab):
        return ab.replace("item_", "").replace(npc + "_", "")
    tr = {int(k): v for k, v in rep["track"].items()}
    T = tr[me["idx"]]
    idx_at = {int(t): i for i, t in enumerate(T["t"])}
    my = _n(me["hero_class"])
    allies = [p for p in rep["players"] if p["team"] == team and p["idx"] != me["idx"]]
    enemies = [p for p in rep["players"] if p["team"] != team]
    ally_n = {_n(p["hero_class"]) for p in allies}
    enemy_n = {_n(p["hero_class"]) for p in enemies}
    by_pid = {f"player:{p['idx']}": _n(p["hero_class"]) for p in rep["players"]}
    end = int(rep["meta"].get("end_time") or T["t"][-1])

    def at(p, i):
        t_ = tr[p["idx"]]
        return (t_["x"][i], t_["y"][i]) if i < len(t_["t"]) and t_["alive"][i] else None

    def where(x, y):
        return zone(*norm_xy(x, y, team))

    # --- bucket per-minute facts
    mins = {}

    def M(t):
        return mins.setdefault(int(t // 60), {"casts": {}, "items": {}, "buys": [], "runes": [], "dmg_out": 0,
                                               "dmg_in": 0, "lines": []})

    for e in rep.get("events") or []:
        t, kind, src, tgt, what, val = e
        if t < -90 or t > end:
            continue
        if kind == "cd" and _n(src) == my and not what.startswith("item_"):
            c = M(t)["casts"]
            c[what] = c.get(what, 0) + 1
        elif kind == "item" and _n(src) == my:
            it = what.replace("item_", "")
            c = M(t)["items"]
            c[it] = c.get(it, 0) + 1
            if it == "smoke_of_deceit":
                M(t)["lines"].append((t, "you used Smoke of Deceit"))
        elif kind == "item" and what == "item_smoke_of_deceit" and _n(src) in ally_n:
            p = next(p for p in allies if _n(p["hero_class"]) == _n(src))
            i = idx_at.get(int(t))
            near = i is not None and T["alive"][i] and (q := at(p, i)) and \
                ((q[0] - T["x"][i]) ** 2 + (q[1] - T["y"][i]) ** 2) ** .5 < 1200
            M(t)["lines"].append((t, f"ally {display(src)} used Smoke ({'you were in it' if near else 'you were NOT nearby'})"))
        elif kind == "item" and what == "item_smoke_of_deceit" and _n(src) in enemy_n:
            M(t)["lines"].append((t, f"enemy {display(src)} used Smoke"))
        elif kind == "buy" and _n(src) == my:
            it = what
            if costs.get(it, 0) >= 1000 or it in ("blink", "black_king_bar", "aghanims_shard", "ultimate_scepter",
                                                     "smoke_of_deceit", "dust", "gem", "ward_observer",
                                                     "ward_sentry", "ward_dispenser"):
                M(t)["buys"].append(it)
        elif kind == "dmg" and _n(src) == my and _n(tgt) in enemy_n:
            M(t)["dmg_out"] += val
        elif kind == "dmg" and _n(tgt) == my:
            M(t)["dmg_in"] += val
        elif kind == "rune":
            who = by_pid.get(src)
            if who == my:
                M(t)["runes"].append(RUNES.get(what, what) + (" (bottled)" if val else ""))
            elif who in enemy_n and RUNES.get(what) not in ("bounty", "water", "wisdom"):
                M(t)["lines"].append((t, f"enemy {display(who)} took a {RUNES.get(what, what)} rune"))
        elif kind == "obj":
            tn = tgt
            ours = (val == team)
            label = ("roshan" if "roshan" in tn else "tormentor" if "tormentor" in tn else
                     tn.replace("goodguys_", "").replace("badguys_", ""))
            i = idx_at.get(int(t))
            dist = None
            if i is not None and T["alive"][i]:
                # distance to the building isn't known; use distance to the killer if it's a hero
                kp = next((p for p in rep["players"] if _n(p["hero_class"]) == _n(src)), None)
                if kp and (q := at(kp, i)):
                    dist = ((q[0] - T["x"][i]) ** 2 + (q[1] - T["y"][i]) ** 2) ** .5
            near = "" if dist is None else (" — you were there" if dist < NEAR else f" — you were {int(dist)} away")
            if "roshan" in tn or "tormentor" in tn:
                side = "your team" if _n(src) in ally_n or _n(src) == my else "ENEMY" if _n(src) in enemy_n else src
                M(t)["lines"].append((t, f"{label.upper()} killed by {side} ({display(src)}){near}"))
            else:
                M(t)["lines"].append((t, f"{'OUR' if ours else 'ENEMY'} {label} destroyed by {display(src)}{near}"))
        elif kind == "buyback" and _n(src or "") == my:
            M(t)["lines"].append((t, "you bought back"))

    # wards placed by the team; attributed to the player if they stood on the spot
    for w in rep.get("wards") or []:
        if w["team"] != team or w["placed"] < -90:
            continue
        i = idx_at.get(int(w["placed"]))
        mine = i is not None and T["alive"][i] and ((w["x"] - T["x"][i]) ** 2 + (w["y"] - T["y"][i]) ** 2) ** .5 < 600
        if mine:
            M(w["placed"])["lines"].append((w["placed"], f"you placed an {w['type']} ward at {where(w['x'], w['y'])}"))

    # kills / assists / deaths
    hits = {}
    for e in rep.get("events") or []:
        if e[1] in ("dmg", "debuff") and _n(e[2]) == my and _n(e[3]) in enemy_n:
            hits.setdefault(_n(e[3]), []).append(e[0])
    for d in rep["deaths"]:
        v = _n(d["victim"])
        if v in enemy_n:
            inv = _n(d["killer"]) == my or any(d["t"] - 12 <= h <= d["t"] for h in hits.get(v, []))
            if inv:
                M(d["t"])["lines"].append((d["t"], f"{'you killed' if _n(d['killer']) == my else 'you assisted on'} "
                                                   f"{display(v)} at {where(d['x'], d['y'])}"))
    for d in rec.get("deaths") or []:
        M(d["t"])["lines"].append((d["t"], f"YOU DIED at {d['zone']} to {display(d['killer'])} — allies near "
                                           f"{d['allies_near']}, enemies near {d['enemies_near']}"
                                           + (f", visible to enemy {d['seen_s_of_15']}/15 s before"
                                              if d.get("seen_s_of_15") is not None else "")))
    for r in (rec.get("rotations") or {}).get("list") or []:
        M(r["left"])["lines"].append((r["left"], f"left own lane → {r['to']}{' (TP)' if r['tp'] else ''}, "
                                                 f"away {r['away_s']} s" + (f", kills: {', '.join(r['kills'])}"
                                                                           if r["kills"] else "")
                                                 + (" — died" if r["died"] else "")))
    for f in (rec.get("fights") or {}).get("list") or []:
        if not f.get("present"):
            if f.get("absent", "").startswith("alive"):
                i = idx_at.get(int(f["onset"]))
                wz = where(T["x"][i], T["y"][i]) if i is not None else "?"
                M(f["onset"])["lines"].append((f["onset"], f"fight elsewhere ({f.get('deaths_enemy', '?')}-"
                                                            f"{f.get('deaths_ally', '?')}), you were "
                                                            f"{f.get('dist_at_onset', '?')} away in {wz}"))
            continue
        sp = ", ".join(f"{short(c['what'])}+{c['t']:g}s"
                       + (f"({c['hits']})" if c.get("hits") else "") for c in f.get("casts", [])[:8])
        mv = f.get("move") or {}
        tot = sum(mv.values())
        bits = [f"FIGHT {'won' if f['won'] else 'lost'} {f['deaths_enemy']}-{f['deaths_ally']}",
                f"engage +{f['engage_delay']}s" if f.get("engage_delay") is not None else "never engaged",
                "first on team" if f.get("first_on_team") else "",
                f"depth {f['depth']}" if f.get("depth") is not None else "",
                f"in/out {round(100 * mv.get('in', 0) / tot)}/{round(100 * mv.get('out', 0) / tot)}%" if tot else "",
                "YOU DIED" if f.get("died") else "",
                f"used: {sp}" if sp else "used nothing",
                f"ready but unused: {', '.join(short(x) for x in f['unused'])}" if f.get("unused") else ""]
        M(f["onset"])["lines"].append((f["onset"], " · ".join(b for b in bits if b)))

    # --- render
    out = []
    w = out.append
    w(f"# {display(me['hero_class'])} — match {rec.get('match_id')} — {'WIN' if rec.get('win') else 'LOSS'} — "
      f"{rec.get('duration_min')} min — {rec.get('team')} — role {rec.get('role')} (team GPM rank "
      f"{rec.get('team_gpm_rank')}), early lane {rec.get('lane_role')} — K/D/A {'/'.join(map(str, rec.get('kda') or []))}")
    w(f"allies: {', '.join(display(p['hero_class']) for p in allies)} | enemies: "
      f"{', '.join(display(p['hero_class']) for p in enemies)}")
    lo = rec.get("laning") or {}
    if lo:
        w(f"lane opponents: {', '.join(lo.get('opponents') or []) or '?'}; lane partners: "
          f"{', '.join(lo.get('partners') or []) or 'none'}")
    w("Per minute (values at the END of the minute): level · net worth (rank in team) · team net-worth lead · "
      "LH (+gained this minute in lane/jungle) · DN; then: where · allies with you (≥40% of the minute) · enemies "
      "within 1500 (≥5 s) · hero dmg dealt/taken · spell casts · active items used · buys · runes; then events.")
    for mm in range(0, end // 60 + 1):
        s0, s1 = mm * 60, min(end, mm * 60 + 59)
        idx = [idx_at[t] for t in range(s0, s1 + 1) if t in idx_at]
        if not idx:
            continue
        alive = [i for i in idx if T["alive"][i]]
        last = idx[-1]
        zc = {}
        for i in alive:
            z = where(T["x"][i], T["y"][i])
            zc[z] = zc.get(z, 0) + 1
        zs = ", ".join(f"{z} {round(100 * c / len(alive))}%" for z, c in sorted(zc.items(), key=lambda x: -x[1])
                       if c / len(alive) >= 0.15) if alive else ""
        dead = len(idx) - len(alive)
        near_a, near_e = {}, {}
        for i in alive:
            here = (T["x"][i], T["y"][i])
            for p in allies:
                if (q := at(p, i)) and ((q[0] - here[0]) ** 2 + (q[1] - here[1]) ** 2) ** .5 < NEAR:
                    near_a[p["hero_class"]] = near_a.get(p["hero_class"], 0) + 1
            for p in enemies:
                if (q := at(p, i)) and ((q[0] - here[0]) ** 2 + (q[1] - here[1]) ** 2) ** .5 < NEAR:
                    near_e[p["hero_class"]] = near_e.get(p["hero_class"], 0) + 1
        na = [display(k) for k, v in near_a.items() if v >= 0.4 * max(1, len(alive))]
        ne = [display(k) for k, v in near_e.items() if v >= 5]
        lh_lane = lh_jg = 0
        for i in idx:
            if i > 0 and T["lh"][i] > T["lh"][i - 1]:
                z = where(T["x"][i], T["y"][i])
                if "jungle" in z:
                    lh_jg += T["lh"][i] - T["lh"][i - 1]
                else:
                    lh_lane += T["lh"][i] - T["lh"][i - 1]
        team_nw = sum(tr[p["idx"]]["nw"][last] for p in allies if last < len(tr[p["idx"]]["nw"])) + T["nw"][last]
        enemy_nw = sum(tr[p["idx"]]["nw"][last] for p in enemies if last < len(tr[p["idx"]]["nw"]))
        my_rank = 1 + sum(1 for p in allies if last < len(tr[p["idx"]]["nw"]) and tr[p["idx"]]["nw"][last] > T["nw"][last])
        b = mins.get(mm, {})
        head = (f"[{mm:02d}:00] lvl {T['level'][last]} · NW {T['nw'][last]} (#{my_rank}) · team {team_nw - enemy_nw:+d} · "
                f"LH {T['lh'][last]} (+{lh_lane}/{lh_jg}) DN {T['dn'][last]}")
        w(head)
        det = []
        if dead:
            det.append(f"dead {dead}s")
        if zs:
            det.append(f"in {zs}")
        if na:
            det.append(f"with {', '.join(na)}")
        if ne:
            det.append(f"enemies near: {', '.join(ne)}")
        if b.get("dmg_out") or b.get("dmg_in"):
            det.append(f"dmg {b.get('dmg_out', 0)}/{b.get('dmg_in', 0)}")
        if b.get("casts"):
            det.append("casts " + ", ".join(short(k) + (f"×{v}" if v > 1 else "") for k, v in b["casts"].items()))
        items_used = {k: v for k, v in (b.get("items") or {}).items() if k not in TOGGLES}
        if items_used:
            det.append("items " + ", ".join(k + (f"×{v}" if v > 1 else "") for k, v in items_used.items()))
        if b.get("buys"):
            det.append("bought " + ", ".join(b["buys"]))
        if b.get("runes"):
            det.append("rune " + ", ".join(b["runes"]))
        if det:
            w("   " + " · ".join(det))
        for t, line in sorted(b.get("lines") or [], key=lambda x: x[0]):
            w(f"   {_t(t)} {line}")
    return "\n".join(out)
