---
name: dota-coach
description: Coach the user on their Dota 2 games (built around Turbo). Finds their recent matches, finds high-MMR Turbo games on the same hero with the most similar drafts, parses the actual .dem replays locally (movement every second, deaths, wards, combat log, item purchases, skill/talent order) and compares the user to those players on movement, economy, item build order, skills/talents and teamfights (initiating vs joining late, who they hit, spell use) to say what they did differently or wrong. Use when the user asks to review / analyse / coach a Dota game, asks "what did I do wrong", or wants comparisons with better players.
---

# Dota coach

Compare one of the user's games against strong players (Turbo games whose average rank tier is ≥ 72, i.e. Divine 2+ average lobbies; ≥ 75 = Immortal-average) who played **the same hero** in **the most similar drafts**. The comparison covers **map movement**, economy, item build order, skills and talents, **teamfights** and deaths.

Everything runs through `scripts/coach.py`, which uses only the Python standard library. You also need `go` to build the replay parser on first use, and `zstd`. Results are cached in `~/.cache/dota-coach/`.

```bash
S=~/.copilot/skills/dota-coach/scripts
python3 $S/coach.py games --n 15                 # recent games: match id, hero, result, K/D/A, replay availability
python3 $S/coach.py compare <MATCH_ID> [<MATCH_ID> ...]  # full report; several ids = your games pooled vs a peer cloud
                                                 # (~1–3 min uncached; seconds when cached)
python3 $S/coach.py timeline <MATCH_ID> [--hero ID]    # minute-by-minute text digest (for the holistic read)
python3 $S/coach.py peers <MATCH_ID>             # just list candidate peer games and their draft overlap
python3 $S/coach.py analyze <MATCH_ID> [--hero ID]  # raw metrics JSON for one player in one game
```

`compare` options:

- `--k 6` sets the number of peer games (per game when several are given). Each uncached peer costs one ~20–35 MB replay download.
- `--days 12` sets how far back to search.
- `--min-rank 72` is the default. At 72 there are about 4× as many candidate games as at 75, so the overlap with your draft is closer (in one test, 9 games shared 4 of the 9 other heroes, against 2 at rank ≥ 75). Use 75 for stronger but less similar peers.
- `--role auto|any|core|support` (default `auto`) keeps only peers who played the same role as you did in that game. Role = the player's net-GPM rank in their team (1–3 core, 4–5 support), read from OpenDota's basic match data before any replay is downloaded. `auto` uses your role per game. Filtering checks about 12×k candidates per game; the report says what share of all checked high-MMR candidates played each role. **If the user's role is the less common one, say so, but still coach within that role.** Peer games shorter than 15 min (abandons, stomps) are skipped.
- `--lane auto|any|safe|mid|off|jungle` (default `auto`) also requires the peer's early-game lane (where they spent most of 0–5 min) to match yours, so an offlane game isn't compared with a mid game on the same hero. Lane is only known after parsing, so mismatches cost replay downloads (from Valve) but no OpenDota requests. Up to 20×k candidates per game are tried. The report gives the lane split of parsed candidates.
- `--peers id,id` lets you supply the peer games yourself. They're pooled under the first game, without draft-overlap scores; with `--role auto`, your most common role is used. Without a key, if OpenDota starts returning 429 to every request (even a lone `curl`), the daily free quota is spent. Stop the run and rerun `compare` with `--peers <ids of the peers already fetched>` and `--k <count>`, because those are fully cached.
- **API key**: if `OPENDOTA_API_KEY` is set in the environment or exported in `~/.zshrc`, it's sent with every OpenDota call, and the client throttle drops from about 1 call/s to 20/s. Never print the key.
- `--account` is the user's OpenDota account id (Steam32). It defaults to `DOTA_COACH_ACCOUNT` from the environment or an `export DOTA_COACH_ACCOUNT=...` line in `~/.zshrc`. If neither is set, ask the user for their account id (or Steam name, then look it up via OpenDota's `/search?q=`).

Outputs:

- A markdown report on stdout, also saved to `~/.cache/dota-coach/reports/<match>.md`.
- `<match>.json` with all the metrics.
- `<match>_heatmap.png`. **Always view the PNG**: it's the main map-movement evidence. Rows are 0–10 min, 10–20 min and 20+ min. The left column is the user and the right column is all peers pooled. Positions are shown from the Radiant perspective, so your own base is bottom-left. Red X marks deaths, the blue diagonal is the river, the grey diagonal is mid, and the grey edge lines are approximate lanes.

## Workflow

1. If the user didn't name a game, run `games` and pick the one they mean (by default the latest). Replays are only downloadable for about 2 weeks.
2. Run `compare <match>` with `initial_wait` ≥ 300. Then read the report and view the heatmap PNG.
3. Write the coaching response:
   - **Lead with the 3–5 biggest, most actionable differences.** Rank them by how much they plausibly cost, not by how far the number deviates.
   - Each point should be concrete. For example: "From 5–10 min you spent 39% of your time in your own jungle; peers on this hero were in the offlane or rotating to the river (river 4% for you vs …)". Then say what to do instead.
   - Cover movement and positioning first: where they were, when, with whom, and where they died. Then cover farm and economy, item timings and choices, and the skill build.
   - Name what they did *well* when it clearly beat the peers. Keep it brief.
   - Treat it as a statistical comparison, not gospel. See the caveats below.
   - **After the headline, always cover each theme in its own short section**, even when nothing stands out (say so in one line):
     - **Laning**: last hits and denies, distance to creeps, damage dealt and taken (including tower damage), regen use.
     - **Rotations**: timing, kills, deaths, TP use, time out of lane.
     - **Fights**:
       - who starts them: first on your team, opened the fight, your team engaged first, engage delay;
       - positioning: depth, distance to the nearest enemy, died first, moving in or out at low HP;
       - who they hit (by enemy net-worth rank);
       - spell order and timing: openers, first-use timing per spell, gap between the 1st and 2nd spell;
       - spells and items left unused, or still ready when they died.
     - **Deaths**: alone vs with team, zones, how long they were visible.
     - **Movement and farm** by phase.
     - **Items, skills and talents.**
     - **Holistic read**: what peers do that the user doesn't.

     Give "you vs peers vs peer wins" numbers, and say which differences are noise.
4. If the user asks for a holistic read ("what do good players do that I don't"), follow *Holistic read* below.

## Holistic read

The metrics only show what was measured. The holistic read finds what wasn't. `compare` writes one text digest per game to `reports/<tag>_timelines/`: `you_<match>_<W|L>.txt` and `peer_<match>_<W|L>.txt`. Each is about 3–4k tokens for a 25-minute game. To print a single game, use `coach.py timeline MATCH [--hero ID]`.

**Digest format.** The header gives the hero, result, side, role and lane, K/D/A, allies and enemies, and lane opponents. Then there is one block per minute. The block line gives values at the end of the minute: level, net worth (rank in team), team net-worth lead, LH (+lane/+jungle gained) and DN. The second line gives:
- the zones occupied ≥15%;
- allies with them ≥40% of the minute;
- enemies within 1500 for ≥5 s;
- hero damage dealt and taken;
- spell casts;
- active items used;
- big buys;
- runes.

Timestamped event lines follow: smokes (theirs, allies' (in it or not), enemies'), enemy power runes, objectives (ours/enemy, with their distance), wards they placed, kills, assists, deaths (who was near, how long they'd been visible), rotations, fights (won/lost, engage delay, depth, in/out, spells with hit counts, ready-but-unused) and fights elsewhere (with their distance).

**Workflow.**
1. Read all of the user's digests and the peer **wins** in the same position. Use 4–6 peers, preferring the highest net-worth rank or the closest draft. If there are more than about 8 games, give each subagent 2–3 digests, asking for a list of habits per game with timestamps, then merge the lists.
2. Look for recurring behaviours peers show that are absent or rare in the user's games. Examples:
   - what they do in the minute after laning ends;
   - where they are when objectives fall;
   - whether they join smokes;
   - how they use the time after a won fight (push or Roshan vs farm);
   - item and power spikes followed by aggression;
   - farming patterns between fights;
   - when they show on the map before dying;
   - TP usage;
   - stacking or jungling;
   - rune control.
3. Write **"What peers do that you don't"** as 3–5 points. Each point must:
   - cite **≥2–3 peer games with timestamps** (e.g. "<match id> @14:10 took T2 with team 40 s after the fight");
   - show the contrast in the user's games, again with timestamps;
   - give a concrete instruction.

   Then add 1–2 things the user does that peers don't, good or bad.
4. Cross-check each claim against the report metrics. If a point contradicts the numbers, say so or drop it. Note patterns worth turning into new metrics.
5. Caveat it: 3 of the user's games against ~5 peers is anecdotal. Peers' lobbies are stronger, and digests only show what the parser sees (no creep-wave state, no vision beyond the player's wards, no item cooldowns between deaths).

## Interpreting the metrics

- **Zones** are approximate 7.41 regions in the Radiant perspective:
  - "safe lane" = bottom/right edge;
  - "off lane" = top/left edge;
  - own/enemy jungle = either side of the river.

  The zones are rectangles and diagonals, not exact map geometry.
- **on enemy half, no ally near**: time on the enemy side of the river with no allied hero within 1500 units. This is the main "overextending / farming dangerously" signal.
- **avg allies within 1500**: how grouped up they are.
- **fight presence %**: the share of all hero deaths (either team, excluding the player's own) where the player was alive within 1800 units. A low value means missing fights.
- **units moved / alive sec**: a rough measure of activity. Low values suggest idling or standing in lane; very high values suggest a lot of walking without farming.
- **avg unspent gold**: gold banked while alive. Persistently high means slow spending or a delayed item; in Turbo, gold also comes fast.
- **teleports**: jumps over 1500 units in one second (TP, blink, other mobility). This is a rough count.
- **Where last hits came from**: which zones the farm came from.
- **Item build order**: "major" items = completed items in a fixed set (blink, BKB, shard, aghs, ...) or ≥ 2000 gold, or composite ≥ 1000. Slot = position in that sequence. Compare both slot and time; a later time with the same slot means slower farm, a different slot means a different build priority.
- **Skills**: first 10 picks, the order in which basic abilities hit max level, and talent pick rates (talent names come from OpenDota constants).
- **Teamfights** (`fights.py`): a fight = hero deaths clustered within 18 s and 4000 units. Onset = walk back from the first death through the chain of hero-vs-hero events (damage, casts on heroes, debuffs) with ≤ 6 s gaps. Present = the player dealt/took hero damage in the fight or was within 1500 units.
  - *engage delay*: seconds from onset to the player's first hostile action (damage, debuff, cast on an enemy). *joined late* = > 5 s or never acted while present.
  - *first on your team* / *opened the fight*: the player's first action came first among allies / among all 10 heroes. For initiators (Axe, Magnus, ...) a low value against peers is a key finding.
  - *died first on your team*: often a positioning signal.
  - *who you hit*: share of fight hero damage by the enemy's net-worth rank at onset (1 = richest = usually their carry).
  - *spells and items*: % of present fights where each was used, casts per fight, enemy heroes hit per cast (debuff/damage with that inflictor within 2 s), median first-use time after onset. Hits for no-damage spells are approximate.
  - The per-fight list shows what the player did in each fight; use it for concrete examples ("at 12:40 you Called 4 s after the fight started").
- **Laning (0–7:00, `laning.py`)**:
  - *opponents/partners*: heroes within 1500 units for ≥ 40% of 1:00–6:00. A "?" means the player roamed or moved between lanes.
  - LH/DN at 2, 4 and 7 min. *of which you last-hit %* = the share of enemy lane creeps dying within 600 units of you that you last-hit. This measures how much of the available farm you took, which matters more than raw LH for supports.
  - Damage dealt/taken per minute, split by heroes, lane creeps and towers. *right-clicks* = auto-attack damage on heroes, i.e. harass.
  - *spell casts / min* and *hit a hero %*: every cast, including ground-targeted ones and items (from cooldown jumps), counts as a hit if the same spell damaged or debuffed an enemy hero within 3 s.
  - Regen/stick uses; average HP; time below 30% HP.
  - *ahead of your own creep front*: median distance from your allied lane creeps toward the enemy (negative = behind your creeps). *time > 150 in front* is the "standing too far up" signal.
  - *seen by enemy %*: see Vision below.
- **Early rotations (0:30–10:00, `rotations.py`)**:
  - *Own lane* = the lane the player was in most during 1:00–4:00.
  - A *rotation* is one trip away from the own lane, ending on return, death or reaching base, that spends ≥ 4 s in another lane. It's listed as `left-lane-time→destination(s)`, marked (TP) if there was a > 1500-unit jump on the way, with the kills the player was involved in (damage/debuff on the victim in the 12 s before death, or the last hit) and whether they died.
  - The report also gives % of alive time out of lane from 2:00, involvement in all early enemy deaths, and involvement in enemy deaths in *other* lanes.
  - The report lists the rotations from up to 6 winning peers, so you can quote concrete timings ("peers rotated mid at 4:30 with TP and got two kills").
  - Few rotations plus low other-lane involvement, against peers who rotate and convert, is a key early-game finding.
- **Vision (approximate)**: a hero counts as "seen" in a given second if they're within vision range of an alive enemy hero, lane creep, tower or observer ward, using day/night ranges. Trees, cliffs and high ground, invisibility, summons, sentries and true-sight items are **ignored**, so treat this as an upper bound and as a relative measure only.
  - It's reported per phase (overall and while on the enemy half), for the 10 s before each fight, and for the 15 s before each death (*seen by enemy, s of last 15*).
  - Dying after 14–15 s of being visible = walked into an enemy that could see you. Dying with low visibility = a gank from fog.
- **Teamfight granularity (`fights.py`)**:
  - *depth*: 0 = the player was the ally closest to the enemy centroid, 1 = furthest back, measured at 4 Hz during the fight.
  - *moving toward/away %*: fraction of fight time with velocity toward or away from the enemy centroid > 120 u/s; blinks are ignored. The *below 30% HP* versions show whether the player bails out or keeps diving when low.
  - *gap between 1st and 2nd spell*: combo speed.
  - *opening sequences*: the first two spells/items used in a fight, for the player, all peers and winning peers.
  - *spells left unused*: active abilities/items that were learned, off cooldown, with enough mana (charges > 0 for charge-based ones) at the fight's last death or at the player's death, and not used during the fight. *died with it ready* is the key "died with buttons unpressed" signal.
  - The per-fight list adds depth, in/out %, seen-before % and unused spells.
- **Deaths**: each death shows the zone and the allies and enemies within 1500 units. A death with 0 allies near and several enemies is usually a positioning or map-awareness error. A death in a 5v5 fight is often fine.

- **Mode / role header**: the report states the game mode and the approximate position (e.g. "pos 3 offlane core") of the player and peers. Position = GPM-rank role + the lane spent most of 0–5 min in. Always state these in the coaching summary.
- **Cost**: the top and the `## Cost` footer give the number of OpenDota API requests this run (including retries). Mention it when it's large.

## Caveats to keep in mind (mention when relevant)

- Peers are matched only on the hero plus overlap of allies and enemies. Typically 2–4 of the 9 other heroes match. Their lobbies are much stronger, so the opponents also play better.
- Peer games include losses. Check whether a pattern holds in the peers' **wins**; the report shows W/L per peer.
- Game length differs: a 15-minute stomp has no "20m+" data. Compare per-minute rates and same-phase numbers, not totals.
- Role is filtered by GPM rank (`--role`), but lane can still differ (e.g. offlane vs safe-lane support). Check the opponents in the laning section and the 0–5 min zones before criticising lane choice. In Turbo a "support" by GPM rank can still be building core items. Look at the peers' item builds.
- Fights are only detected when a hero dies; skirmishes without kills are invisible.
- Medians of about 6 games are noisy. Highlight large, consistent gaps; don't over-read small ones.

## Internals

- `parser/main.go` is a Go program built on dotabuff/manta. It turns a `.dem` replay into JSON (meta.version 5): per-second hero x/y, alive, HP, mana, level, gold, net worth, LH and DN; hero deaths with victim position; observer/sentry wards; `events` from the combat log (`[t, kind, src, target, inflictor, value]`, kinds dmg/cast/item/debuff/buy/buyback, hero-related only); `skills` (ability level-ups from ability entities); and per-player totals (K/D/A, gold, XP, hero/tower damage). v3 adds:
  - `vis` (1 Hz, approximately seen by the enemy);
  - `q/qx/qy/qhp` (4 Hz position and HP% for fight movement);
  - `"cd"` events (`[t, "cd", hero_class, "", ability_or_item, cooldown]`), emitted whenever a cooldown starts, so every cast is caught, including ground-targeted spells;
  - `states` (each hero's mana and abilities/items with level, cooldown remaining, mana cost and charges, snapshotted at every hero death);
  - `creeps` / `creep_deaths` (lane creep positions and deaths until 15:00).
  - v4 adds `obj` events (`[t,"obj",killer,building,"",team]`, which cover towers, barracks, Ancient, Roshan and Tormentor; an ally killing its own building is a deny) and `rune` events (`[t,"rune","player:<idx>","",rune_type,bottled]`, from chat events, because combat-log rune pickups are empty). Rune types: 0 DD, 1 haste, 2 illusion, 3 invis, 4 regen, 5 bounty, 6 arcane, 7 water, 8 wisdom, 9 shield.
  - v5 fixes a nil-hero crash in ability tracking (summon-owned abilities), which made manta stop early and silently truncate some replays (e.g. a 23-minute game parsed as 3.6 minutes). manta recovers callback panics and prints `parse error:` to stderr, so `odota.replay()` now treats that as a failure rather than caching a partial parse.
  - Parsing takes about 2 s per replay; the JSON is about 5 MB.
  - Game time is `(server_tick − paused_ticks)/30 − m_flGameStartTime`. It matches OpenDota's clock.
  - World coordinates are `cell*128 + vec − 16384`. The fountains are at about (−7000, −6500) for Radiant and (7000, 6500) for Dire.
- `scripts/odota.py` handles the OpenDota API.
  - OpenDota is only needed for match history, the peer search and the **replay salt** (Valve replay URL). `match()` POSTs `/request/{id}` and polls only until `replay_salt` appears (~5–10 s); it does not wait for OpenDota's full parse.
  - The replay is downloaded from Valve (zstd-compressed despite the `.dem.bz2` name), kept in `cache/replays/{id}.dem.zst`, decompressed and parsed. Parsed JSON with `meta.version` < `PARSE_VERSION` is re-parsed from the kept replay, so bump `PARSE_VERSION` when changing the parser. Some replay clusters (e.g. Chinese) are unreachable; downloads fail after 2×45 s and the peer is skipped.
- `scripts/analyze.py` computes the metrics (with `laning.py`, `fights.py` and `rotations.py`), plus `timeline.py` for the digest; `scripts/mapdraw.py` draws the PNG heatmaps (pure Python).
- Peer search uses the OpenDota Explorer on `public_matches` (game_mode 23, avg_rank_tier ≥ min-rank, duration ≥ 10 min). Candidates are ranked by draft overlap (number of shared allies plus shared enemies), then by rank, then by recency. It queries 2-day match_id windows because larger ranges time out. The search runs once per hero and is shared by all games in a multi-game compare.
