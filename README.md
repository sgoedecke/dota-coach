# dota-coach

A Copilot / agent skill that coaches a Dota 2 player on their own games (built around Turbo).

For a given match it finds high-MMR Turbo games on the same hero with the most similar drafts, same role and same early lane, downloads and parses the actual `.dem` replays locally (Go + [manta](https://github.com/dotabuff/manta)), and compares the player with those peers on laning, rotations, teamfights (who starts them, positioning, targets, spell order, unused spells), deaths, movement, farm, items and skills. It also writes minute-by-minute text timelines of every game so an agent can do a holistic "what do good players do that I don't" read.

See [SKILL.md](SKILL.md) for usage, the workflow and how to interpret the metrics.

## Requirements

- Python 3 (standard library only)
- Go (the replay parser builds itself on first use into `~/.cache/dota-coach/bin/`)
- `zstd`

## Install

```bash
git clone <this repo> ~/Code/dota-coach
ln -s ~/Code/dota-coach ~/.copilot/skills/dota-coach
```

## Configuration

Set these in the environment, or as `export NAME=...` lines in `~/.zshrc`:

- `DOTA_COACH_ACCOUNT`: your OpenDota account id (Steam32). Or pass `--account` to every command.
- `OPENDOTA_API_KEY` (optional): raises the OpenDota rate limit; without it you're limited to the free daily quota.
- `DOTA_COACH_CACHE` (optional): cache directory, default `~/.cache/dota-coach`.

## Quick start

```bash
S=~/.copilot/skills/dota-coach/scripts
python3 $S/coach.py games --n 15
python3 $S/coach.py compare <MATCH_ID> [<MATCH_ID> ...]
```

Replays are only downloadable from Valve for a few weeks after the match.
