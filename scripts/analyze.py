"""Per-player metrics from a parsed replay (movement track) + OpenDota match JSON.

All positions are normalised to the Radiant perspective (Dire coordinates are rotated 180 degrees),
so "own base" is always bottom-left, "safe lane" is bottom/right, "off lane" is top/left.
"""
import math

from fights import fight_analysis, _npc
from laning import lane_analysis
from rotations import rotations
from timeline import timeline

PHASES = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 999)]  # minutes; turbo games are short
NEAR = 1500  # units: "with an ally" / "enemy nearby"
FIGHT_R = 1800  # a hero death within this distance counts as a fight you were present for
ZONES = ["own base", "safe lane", "mid lane", "off lane", "own jungle", "river", "enemy jungle", "enemy base"]


def phase_name(i):
    a, b = PHASES[i]
    return f"{a}-{b}m" if b < 999 else f"{a}m+"


def phase_of(t):
    m = t / 60
    for i, (a, b) in enumerate(PHASES):
        if a <= m < b:
            return i
    return None


def norm_xy(x, y, team):
    return (x, y) if team == 2 else (-x, -y)


def zone(x, y):
    """Coarse map zone in Radiant perspective (approximate 7.41 geometry)."""
    if x < -3600 and y < -3300:
        return "own base"
    if x > 3300 and y > 2900:
        return "enemy base"
    if y < -5200 or x > 5200:
        return "safe lane"
    if x < -5200 or y > 5000:
        return "off lane"
    if abs(x - y) < 1100:
        return "mid lane"
    if abs(x + y) < 1100:
        return "river"
    return "own jungle" if x + y < 0 else "enemy jungle"


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def find_player(rep, account_id=None, hero_id=None):
    for p in rep["players"]:
        if account_id and p["account_id"] == account_id:
            return p
    for p in rep["players"]:
        if hero_id and p["hero_id"] == hero_id:
            return p
    raise KeyError("player not found in replay")


def item_costs():
    from odota import constants
    return {k: v.get("cost") or 0 for k, v in constants("items").items()}


CONSUMABLES = {"tango", "flask", "clarity", "enchanted_mango", "faerie_fire", "ward_observer", "ward_sentry",
               "ward_dispenser", "tpscroll", "dust", "smoke_of_deceit", "blood_grenade", "tome_of_knowledge",
               "bottle", "aghanims_shard_roshan", "cheese", "refresher_shard", "famango", "great_famango",
               "greater_famango"}
ALWAYS_MAJOR = {"blink", "aghanims_shard", "ultimate_scepter", "black_king_bar"}


def npc_of(rep, p):
    """combat-log name ("phantom_assassin") of a replay player."""
    want = p["hero_class"].replace("CDOTA_Unit_Hero_", "").replace("_", "").lower()
    names = {e[2] for e in rep.get("events") or [] if e[1] in ("cast", "buy", "item")}
    for n in names:
        if n.replace("_", "").lower() == want:
            return n
    return want


def item_build(rep, npc):
    """(items >= 1000 gold with first purchase time, ordered list of major completed items)."""
    from odota import constants
    C = constants("items")
    items, build, seen = [], [], set()
    for t, kind, src, _, it, _ in rep.get("events") or []:
        if kind != "buy" or src != npc or it.startswith("recipe_") or it in seen:
            continue
        seen.add(it)
        c = C.get(it, {})
        cost = c.get("cost") or 0
        if cost >= 1000:
            items.append({"item": it, "t": t})
        if it in CONSUMABLES:
            continue
        if it in ALWAYS_MAJOR or cost >= 2000 or (c.get("components") and cost >= 1000):
            build.append({"item": it, "t": t})
    return items, build


def skill_build(rep, me):
    """(skill pick order, talents picked, order in which basic abilities hit max level)."""
    mine = [s for s in rep.get("skills") or [] if s["hero"] == me["hero_class"]]
    if not mine:
        return [], [], []
    t0 = min(s["t"] for s in mine)
    mine = [s for s in mine if s["t"] > t0 + 2]  # innate abilities are levelled at spawn
    seq = [s["ability"] for s in mine if s["ability"] != "special_bonus_attributes"]
    talents = list(dict.fromkeys(s["ability"] for s in mine
                                 if s["ability"].startswith("special_bonus") and s["ability"] != "special_bonus_attributes"))
    levels, maxed = {}, []
    for s in mine:
        levels[s["ability"]] = s["level"]
        if s["level"] == 4 and not s["ability"].startswith("special_bonus") and s["ability"] not in maxed:
            maxed.append(s["ability"])
    return seq, talents, maxed


def role_of(m, hero_id):
    """(rank of the hero's GPM within its team 1-5, "core" for ranks 1-3 / "support" for 4-5) from basic match data."""
    ps = m.get("players") or []
    me = next((p for p in ps if p.get("hero_id") == hero_id), None)
    if me is None or me.get("gold_per_min") is None:
        return None, None
    rad = me["player_slot"] < 128
    team = sorted((p for p in ps if (p["player_slot"] < 128) == rad), key=lambda p: -(p.get("gold_per_min") or 0))
    rank = 1 + next(i for i, p in enumerate(team) if p is me)
    return rank, ("core" if rank <= 3 else "support")


def analyze(rep, m, account_id=None, hero_id=None):
    me = find_player(rep, account_id, hero_id)
    team = me["team"]
    tr = {int(k): v for k, v in rep["track"].items()}
    T = tr[me["idx"]]
    n = len(T["t"])
    allies = [p for p in rep["players"] if p["team"] == team and p["idx"] != me["idx"]]
    enemies = [p for p in rep["players"] if p["team"] != team]

    def at(p, i):
        t = tr[p["idx"]]
        return (t["x"][i], t["y"][i]) if i < len(t["t"]) and t["alive"][i] else None

    zone_t = [{z: 0 for z in ZONES} for _ in PHASES]
    alive_t = [0] * len(PHASES)
    dead_t = [0] * len(PHASES)
    enemy_half_t = [0] * len(PHASES)
    enemy_half_alone_t = [0] * len(PHASES)
    seen_t = [0] * len(PHASES)
    seen_enemy_half_t = [0] * len(PHASES)
    V = T.get("vis") or []
    ally_sum = [0] * len(PHASES)
    base_t = [0] * len(PHASES)
    move = [0.0] * len(PHASES)
    tps = []
    lh_zone = {z: 0 for z in ZONES}
    path = []  # normalised positions while alive (for heatmaps)
    for i in range(n):
        ph = phase_of(T["t"][i])
        if ph is None:
            continue
        if not T["alive"][i]:
            dead_t[ph] += 1
            continue
        alive_t[ph] += 1
        x, y = norm_xy(T["x"][i], T["y"][i], team)
        path.append((T["t"][i], x, y))
        z = zone(x, y)
        zone_t[ph][z] += 1
        if z == "own base":
            base_t[ph] += 1
        here = (T["x"][i], T["y"][i])
        na = sum(1 for a in allies if (q := at(a, i)) and dist(q, here) < NEAR)
        ally_sum[ph] += na
        seen = i < len(V) and V[i]
        seen_t[ph] += seen
        if x + y > 0:
            enemy_half_t[ph] += 1
            seen_enemy_half_t[ph] += seen
            if na == 0:
                enemy_half_alone_t[ph] += 1
        if i > 0 and T["alive"][i - 1]:
            d = dist(here, (T["x"][i - 1], T["y"][i - 1]))
            if d > 1500:
                tps.append((T["t"][i], zone(*norm_xy(T["x"][i - 1], T["y"][i - 1], team)), z))
            else:
                move[ph] += d
        if i > 0 and T["lh"][i] > T["lh"][i - 1]:
            lh_zone[z] += T["lh"][i] - T["lh"][i - 1]

    # fights: any hero death; were we alive and nearby?

    def norm_name(s):
        return s.replace("CDOTA_Unit_Hero_", "").replace("npc_dota_hero_", "").replace("_", "").lower()

    my_name = norm_name(me["hero_class"])
    enemy_names = {norm_name(p["hero_class"]) for p in enemies}
    ally_names = {norm_name(p["hero_class"]) for p in allies}
    fights_present = fights_total = 0
    my_deaths = []
    idx_at = {t: i for i, t in enumerate(T["t"])}
    for d in rep["deaths"]:
        v = norm_name(d["victim"])
        if v not in enemy_names and v not in ally_names and v != my_name:
            continue
        i = idx_at.get(int(d["t"]))
        if i is None:
            continue
        if v == my_name:
            here = (d["x"], d["y"])
            nx, ny = norm_xy(d["x"], d["y"], team)
            na = sum(1 for a in allies if (q := at(a, i)) and dist(q, here) < NEAR)
            ne = sum(1 for e in enemies if (q := at(e, i)) and dist(q, here) < NEAR)
            vs = [V[j] for j in range(max(0, i - 15), i) if j < len(V) and T["alive"][j]] if V else []
            my_deaths.append({"t": d["t"], "zone": zone(nx, ny), "x": nx, "y": ny, "allies_near": na,
                              "seen_s_of_15": sum(vs) if V else None,
                              "enemies_near": ne, "killer": d["killer"].replace("npc_dota_hero_", "")})
            continue
        fights_total += 1
        if T["alive"][i] and dist((T["x"][i], T["y"][i]), (d["x"], d["y"])) < FIGHT_R:
            fights_present += 1

    # economy snapshots
    def snap(minute, key):
        i = idx_at.get(minute * 60)
        return T[key][i] if i is not None else None

    econ = {f"{mm}m": {"nw": snap(mm, "nw"), "lh": snap(mm, "lh"), "dn": snap(mm, "dn"), "lvl": snap(mm, "level")}
            for mm in (5, 10, 15, 20, 25) if idx_at.get(mm * 60) is not None}
    gold_float = [round(sum(T["gold"][i] for i in range(n) if phase_of(T["t"][i]) == ph and T["alive"][i]) /
                        max(1, alive_t[ph])) for ph in range(len(PHASES))]

    # items & skills straight from the replay
    my_npc = npc_of(rep, me)
    items, build = item_build(rep, my_npc)
    skills, talents, max_order = skill_build(rep, me)
    fights = fight_analysis(rep, me)
    npc = {p["idx"]: _npc(rep, p) for p in rep["players"]}
    team_of = {npc[p["idx"]]: p["team"] for p in rep["players"]}
    laning = lane_analysis(rep, me, npc, team_of)
    rot = rotations(rep, me, team, zone, norm_xy)
    gpm_rank, role = role_of(m, me["hero_id"])
    lane_role = None
    if phases_early := zone_t[0]:
        lanes = {"safe": phases_early["safe lane"], "mid": phases_early["mid lane"],
                 "off": phases_early["off lane"],
                 "jungle": phases_early["own jungle"] + phases_early["enemy jungle"]}
        if sum(lanes.values()):
            lane_role = max(lanes, key=lanes.get)

    phases = []
    for ph in range(len(PHASES)):
        if alive_t[ph] + dead_t[ph] < 60:  # ignore the last sliver of a phase the game barely reached
            continue
        a = max(1, alive_t[ph])
        phases.append({
            "phase": phase_name(ph),
            "dead_pct": round(100 * dead_t[ph] / (alive_t[ph] + dead_t[ph])),
            "zones_pct": {z: round(100 * c / a) for z, c in zone_t[ph].items() if c},
            "enemy_half_pct": round(100 * enemy_half_t[ph] / a),
            "enemy_half_alone_pct": round(100 * enemy_half_alone_t[ph] / a),
            "avg_allies_near": round(ally_sum[ph] / a, 2),
            "move_speed_eff": round(move[ph] / a),  # units walked per alive second
            "avg_unspent_gold": gold_float[ph],
            "seen_pct": round(100 * seen_t[ph] / a) if V else None,
            "seen_on_enemy_half_pct": round(100 * seen_enemy_half_t[ph] / enemy_half_t[ph])
            if V and enemy_half_t[ph] >= 10 else None,
        })
    dur = rep["meta"].get("end_time") or T["t"][-1]
    result = {
        "match_id": m.get("match_id"),
        "hero_id": me["hero_id"],
        "hero": me["hero_class"].replace("CDOTA_Unit_Hero_", ""),
        "name": me["name"],
        "account_id": me["account_id"],
        "team": "radiant" if team == 2 else "dire",
        "win": (m.get("radiant_win") == (team == 2)) if "radiant_win" in m else None,
        "duration_min": round(dur / 60, 1),
        "kda": [me.get("kills"), me.get("deaths"), me.get("assists")],
        "gpm": round(me.get("total_gold", 0) / (dur / 60)) if dur else None,
        "xpm": round(me.get("total_xp", 0) / (dur / 60)) if dur else None,
        "hero_damage": me.get("hero_damage"), "tower_damage": me.get("tower_damage"),
        "lane_role": lane_role,
        "team_gpm_rank": gpm_rank,
        "role": role,
        "game_mode": m.get("game_mode"),
        "laning": laning,
        "rotations": rot,
        "econ": econ,
        "items": items,
        "build": build,
        "skills": skills,
        "talents": talents,
        "max_order": max_order,
        "fights": fights,
        "phases": phases,
        "lh_by_zone_pct": {z: round(100 * c / max(1, sum(lh_zone.values()))) for z, c in lh_zone.items() if c},
        "teleports": len(tps),
        "fight_presence_pct": round(100 * fights_present / max(1, fights_total)),
        "fights_total": fights_total,
        "deaths": my_deaths,
        "path": path,
    }
    try:
        from odota import hero_names
        result["timeline"] = timeline(rep, me, team, result, zone, norm_xy, item_costs(),
                                      {int(k): v for k, v in hero_names().items()}, my_npc)
    except Exception as e:  # noqa: BLE001 — the digest is optional; never break the metrics
        result["timeline"] = f"(timeline failed: {e})"
    return result
