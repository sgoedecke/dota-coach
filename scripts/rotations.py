"""Early-game rotations: trips from the player's own lane to another lane, and what they produced."""

LANES = ("safe lane", "mid lane", "off lane")
MIN_STAY = 4      # seconds in another lane to count as a rotation
MERGE_GAP = 12    # transit gaps (jungle/river) shorter than this don't end a rotation
INVOLVE_S = 12    # damage/debuff on the victim within this many seconds before the death = involved
NEAR = 1500


def _n(s):
    return s.replace("CDOTA_Unit_Hero_", "").replace("npc_dota_hero_", "").replace("_", "").lower()


def rotations(rep, me, team, zone, norm_xy, until=600):
    """zone/norm_xy: analyze.py helpers (Radiant-perspective zones). Returns a dict of metrics."""
    tr = {int(k): v for k, v in rep["track"].items()}
    T = tr[me["idx"]]
    idx_at = {int(t): i for i, t in enumerate(T["t"])}
    my = _n(me["hero_class"])
    enemies = {_n(p["hero_class"]) for p in rep["players"] if p["team"] != team}

    lab = {}  # t -> lane label or None (transit) for alive seconds
    for i, t in enumerate(T["t"]):
        if 30 <= t < until and T["alive"][i]:
            z = zone(*norm_xy(T["x"][i], T["y"][i], team))
            lab[int(t)] = z if z in LANES else None
    home_counts = {}
    for t, z in lab.items():
        if 60 <= t < 240 and z:
            home_counts[z] = home_counts.get(z, 0) + 1
    if not home_counts:
        return None
    home = max(home_counts, key=home_counts.get)

    # my hostile actions on each enemy (for kill involvement)
    hits = {}
    for e in rep.get("events") or []:
        if e[1] in ("dmg", "debuff") and _n(e[2]) == my and _n(e[3]) in enemies and e[0] < until + 30:
            hits.setdefault(_n(e[3]), []).append(e[0])
    enemy_deaths = [d for d in rep["deaths"] if _n(d["victim"]) in enemies and 30 <= d["t"] < until + 15]
    my_deaths = [d["t"] for d in rep["deaths"] if _n(d["victim"]) == my]

    def involved(d):
        if _n(d["killer"]) == my:
            return True
        return any(d["t"] - INVOLVE_S <= h <= d["t"] for h in hits.get(_n(d["victim"]), []))

    # an excursion = everything between leaving the own lane and coming back to it (or dying / reaching base);
    # it counts as a rotation if it spent >= MIN_STAY s in some other lane
    trips, cur = [], None
    for t in range(30, until + 1):
        z = lab.get(t, "dead")
        if z == home or z == "dead" or t == until:
            if cur:
                trips.append(cur)
                cur = None
            continue
        if cur is None:
            cur = {"left": t, "lanes": {}, "first_in": {}}
        cur["last"] = t
        if z:
            cur["lanes"][z] = cur["lanes"].get(z, 0) + 1
            cur["first_in"].setdefault(z, t)
    trips = [r for r in trips if any(v >= MIN_STAY for v in r["lanes"].values())]

    out = []
    for r in trips:
        dests = sorted((z for z, v in r["lanes"].items() if v >= MIN_STAY), key=lambda z: r["first_in"][z])
        arrived = r["first_in"][dests[0]]
        i0, i1 = idx_at.get(r["left"]), idx_at.get(arrived)
        tp = False
        if i0 is not None and i1 is not None:
            for i in range(max(i0, 1), i1 + 1):
                if T["alive"][i - 1] and T["alive"][i] and \
                        ((T["x"][i] - T["x"][i - 1]) ** 2 + (T["y"][i] - T["y"][i - 1]) ** 2) ** 0.5 > 1500:
                    tp = True
        end = r["last"] + 10
        kills = [d for d in enemy_deaths if arrived - 5 <= d["t"] <= end and involved(d)]
        out.append({"left": r["left"], "arrived": arrived, "to": "+".join(z.replace(" lane", "") for z in dests),
                    "travel_s": arrived - r["left"], "away_s": r["last"] - r["left"] + 1, "tp": tp,
                    "kills": [_n(d["victim"]) for d in kills],
                    "died": any(r["left"] <= t <= end for t in my_deaths)})

    alive_2_10 = [t for t in lab if 120 <= t < until]
    away = [t for t in alive_2_10 if lab[t] != home]
    early = [d for d in enemy_deaths if d["t"] < until]
    inv = [d for d in early if involved(d)]
    other_lane = []
    for d in early:
        z = zone(*norm_xy(d["x"], d["y"], team))
        if z in LANES and z != home:
            other_lane.append(d)
    return {
        "until_s": until,
        "home_lane": home.replace(" lane", ""),
        "count": len(out),
        "first_s": out[0]["left"] if out else None,
        "with_kill_pct": round(100 * sum(1 for r in out if r["kills"]) / len(out)) if out else None,
        "died_pct": round(100 * sum(1 for r in out if r["died"]) / len(out)) if out else None,
        "tp_pct": round(100 * sum(1 for r in out if r["tp"]) / len(out)) if out else None,
        "kills_from_rotations": sum(len(r["kills"]) for r in out),
        "away_from_lane_pct": round(100 * len(away) / len(alive_2_10)) if alive_2_10 else None,
        "enemy_deaths": len(early),
        "involved_pct": round(100 * len(inv) / len(early)) if early else None,
        "other_lane_kills": len(other_lane),
        "other_lane_involved": sum(1 for d in other_lane if involved(d)),
        "list": out,
    }
