// Extracts per-second hero movement and key events from a Dota 2 Source 2 replay (.dem) as JSON.
//
// Usage: dotaparse <replay.dem> > out.json
//
// Output:
//
//	meta:    match_id, game_start_sim, total_ticks
//	players: one per hero: player index, name, account_id, team (2 radiant / 3 dire), hero_id, hero class
//	track:   per player, sampled once per second of game time (pauses excluded):
//	         t (game seconds, 0 = horn), x, y (world units, map centre = 0,0), alive, hp, max_hp, mana, level,
//	         gold (unspent), nw (net worth if available), lh, dn
//	deaths:  hero deaths with game time, victim/killer, victim position
//	wards:   observer / sentry wards with placement position, team, placed/removed times
//	         (v3 track also has vis: per-second "approximately seen by enemy", and q/qx/qy/qhp: 4 Hz pos + HP%)
//	events:  combat log [t, kind, src, target, inflictor, value]; kind "cd" = a cooldown started (any cast),
//	         "obj" = building / Roshan / tormentor death (target = unit name, value = its team), "rune" = rune pickup
//	skills:  ability level-ups; totals: per-player end-of-game totals
//	states:  per hero death, every hero's mana + abilities/items [name, lvl, cd_remaining, manacost, charges]
//	creeps / creep_deaths: lane creep positions (1 Hz) and deaths until meta.creeps_until
package main

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"strings"

	"github.com/dotabuff/manta"
	"github.com/dotabuff/manta/dota"
)

const steamBase = 76561197960265728

type Player struct {
	Idx       int    `json:"idx"`
	Name      string `json:"name"`
	AccountID int64  `json:"account_id"`
	Team      int    `json:"team"`
	HeroID    int    `json:"hero_id"`
	HeroClass string `json:"hero_class"`
	TeamSlot  int    `json:"team_slot"`
	Kills     int    `json:"kills"`
	Deaths    int    `json:"deaths"`
	Assists   int    `json:"assists"`
	Gold      int    `json:"total_gold"`
	XP        int    `json:"total_xp"`
	TowerDmg  int    `json:"tower_damage"`
	HeroDmg   int    `json:"hero_damage"`
	entIdx    int32
	pr        int // index in CDOTA_PlayerResource vectors
}

type Track struct {
	T     []int     `json:"t"`
	X     []float32 `json:"x"`
	Y     []float32 `json:"y"`
	Alive []bool    `json:"alive"`
	HP    []int32   `json:"hp"`
	MaxHP []int32   `json:"max_hp"`
	Mana  []float32 `json:"mana"`
	Level []int32   `json:"level"`
	Gold  []int32   `json:"gold"`
	NW    []int32   `json:"nw"`
	LH    []int32   `json:"lh"`
	DN    []int32   `json:"dn"`
	Vis   []bool    `json:"vis"` // approx. seen by the enemy team (enemy heroes, lane creeps, towers, observer wards in range)
	// 4 Hz samples: quarter-second index (t*4), position, HP %
	Q   []int32   `json:"q"`
	QX  []float32 `json:"qx"`
	QY  []float32 `json:"qy"`
	QHP []int8    `json:"qhp"`
}

// State of one hero's usable abilities/items at a moment: [name, level, cooldown remaining, mana cost, charges]
type HeroState struct {
	Hero  string  `json:"hero"`
	Mana  float64 `json:"mana"`
	Alive bool    `json:"alive"`
	Abil  [][]any `json:"abil"`
}

type Snapshot struct {
	T      float64      `json:"t"`
	Reason string       `json:"reason"`
	Heroes []*HeroState `json:"heroes"`
}

type Death struct {
	T      float64 `json:"t"`
	Victim string  `json:"victim"`
	Killer string  `json:"killer"`
	X      float32 `json:"x"`
	Y      float32 `json:"y"`
}

type Skill struct {
	T       float64 `json:"t"`
	Hero    string  `json:"hero"`
	Ability string  `json:"ability"`
	Level   int     `json:"level"`
}

type Ward struct {
	Type    string  `json:"type"`
	Team    int     `json:"team"`
	X       float32 `json:"x"`
	Y       float32 `json:"y"`
	Placed  float64 `json:"placed"`
	Removed float64 `json:"removed"`
	Owner   int     `json:"owner_idx"`
}

func getF(e *manta.Entity, k string) (float64, bool) {
	v := e.Get(k)
	ok := v != nil
	if !ok || v == nil {
		return 0, false
	}
	switch t := v.(type) {
	case float32:
		return float64(t), true
	case float64:
		return t, true
	case int32:
		return float64(t), true
	case uint32:
		return float64(t), true
	case int64:
		return float64(t), true
	case uint64:
		return float64(t), true
	case bool:
		if t {
			return 1, true
		}
		return 0, true
	}
	return 0, false
}

func pos(e *manta.Entity) (float32, float32, bool) {
	cx, ok1 := getF(e, "CBodyComponent.m_cellX")
	cy, ok2 := getF(e, "CBodyComponent.m_cellY")
	vx, ok3 := getF(e, "CBodyComponent.m_vecX")
	vy, ok4 := getF(e, "CBodyComponent.m_vecY")
	if !(ok1 && ok2 && ok3 && ok4) {
		return 0, 0, false
	}
	return float32(cx*128 + vx - 16384), float32(cy*128 + vy - 16384), true
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: dotaparse replay.dem")
		os.Exit(2)
	}
	f, err := os.Open(os.Args[1])
	if err != nil {
		panic(err)
	}
	defer f.Close()
	p, err := manta.NewStreamParser(f)
	if err != nil {
		panic(err)
	}

	var rules, pres *manta.Entity
	dataTeam := map[int]*manta.Entity{} // team -> CDOTA_DataRadiant / CDOTA_DataDire
	heroes := map[int32]*manta.Entity{} // entity index -> hero entity
	var players []*Player
	tracks := map[int]*Track{}
	var deaths []Death
	var events [][]any // [t, kind, source, target, inflictor, value]
	var skills []Skill
	wards := map[int32]*Ward{}
	abilityLvl := map[int32]int{}
	var wardList []*Ward
	creeps := map[int32]*manta.Entity{} // lane creeps (incl. siege)
	creepDead := map[int32]bool{}
	towers := map[int32]*manta.Entity{}
	obsEnt := map[int32]*manta.Entity{}
	abilOwner := map[int32]int32{} // ability entity -> hero entity index (-1 = not a hero ability)
	cdPrev := map[int32]float64{}
	type creepRows struct {
		T    []int     `json:"t"`
		Team []int8    `json:"team"`
		X    []float32 `json:"x"`
		Y    []float32 `json:"y"`
	}
	creepSnap := &creepRows{}
	creepDeaths := &creepRows{}
	var snaps []*Snapshot
	lastQ := math.MinInt32
	const creepUntil = 900.0
	entName := func(e *manta.Entity) string {
		idx, _ := getF(e, "m_pEntity.m_nameStringTableIndex")
		s, _ := p.LookupStringByIndex("EntityNames", int32(idx))
		return s
	}
	lastSec := math.MinInt32
	gameStart := 0.0
	endTime := 0.0
	// p.Tick is the replay's own tick counter, which can be offset from the server tick
	// used by the game rules (m_flGameStartTime, pause ticks); track the server tick separately.
	var serverTick uint32

	gameTime := func() (float64, bool) {
		if rules == nil {
			return 0, false
		}
		gs, _ := getF(rules, "m_pGameRules.m_flGameStartTime")
		if gs <= 0 {
			// during pre-game the state transition time is the upcoming horn
			st, _ := getF(rules, "m_pGameRules.m_nGameState")
			if st != 4 {
				return 0, false
			}
			gs, _ = getF(rules, "m_pGameRules.m_flStateTransitionTime")
			if gs <= 0 {
				return 0, false
			}
			paused, _ := getF(rules, "m_pGameRules.m_nTotalPausedTicks")
			return (float64(serverTick)-paused)/30.0 - gs, true
		}
		gameStart = gs
		paused, _ := getF(rules, "m_pGameRules.m_nTotalPausedTicks")
		tick := float64(serverTick)
		if ps, _ := getF(rules, "m_pGameRules.m_nPauseStartTick"); ps > 0 {
			tick = ps
		}
		return (tick-paused)/30.0 - gs, true
	}

	// owner hero of an ability or item entity (scans heroes' ability and inventory handles; cached once found)
	ownerOfAbility := func(key int32) int32 {
		if o, ok := abilOwner[key]; ok {
			return o
		}
		for hi, h := range heroes {
			for _, pre := range []string{"m_vecAbilities.%04d", "m_hItems.%04d"} {
				for j := 0; j < 35; j++ {
					v, ok := getF(h, fmt.Sprintf(pre, j))
					if !ok {
						break
					}
					if int32(uint64(v)&0x3FFF) == key {
						abilOwner[key] = hi
						return hi
					}
				}
			}
		}
		return -1
	}
	abilRow := func(a *manta.Entity) []any {
		lvl, _ := getF(a, "m_iLevel")
		cd, _ := getF(a, "m_fCooldown")
		mc, _ := getF(a, "m_iManaCost")
		ch, _ := getF(a, "m_nAbilityCurrentCharges")
		rest, _ := getF(a, "m_fAbilityChargeRestoreTimeRemaining")
		if rest < -1000 { // not a charge-based ability
			ch = -1
		}
		return []any{entName(a), int(lvl), math.Round(cd*10) / 10, int(mc), int(ch)}
	}
	snapshot := func(t float64, reason string) {
		s := &Snapshot{T: math.Round(t*10) / 10, Reason: reason}
		for _, pl := range players {
			he, ok := heroes[pl.entIdx]
			if !ok {
				continue
			}
			mana, _ := getF(he, "m_flMana")
			life, _ := getF(he, "m_lifeState")
			hs := &HeroState{Hero: he.GetClassName(), Mana: math.Round(mana), Alive: life == 0}
			for j := 0; j < 35; j++ {
				v, ok := getF(he, fmt.Sprintf("m_vecAbilities.%04d", j))
				if !ok {
					break
				}
				if uint64(v) == 16777215 {
					continue
				}
				a := p.FindEntity(int32(uint64(v) & 0x3FFF))
				if a == nil {
					continue
				}
				if lvl, _ := getF(a, "m_iLevel"); lvl <= 0 {
					continue
				}
				r := abilRow(a)
				if n, _ := r[0].(string); strings.HasPrefix(n, "special_bonus") || n == "" {
					continue
				}
				hs.Abil = append(hs.Abil, r)
			}
			for _, j := range []int{0, 1, 2, 3, 4, 5, 15, 16} {
				v, ok := getF(he, fmt.Sprintf("m_hItems.%04d", j))
				if !ok || uint64(v) == 16777215 {
					continue
				}
				if a := p.FindEntity(int32(uint64(v) & 0x3FFF)); a != nil {
					hs.Abil = append(hs.Abil, abilRow(a))
				}
			}
			s.Heroes = append(s.Heroes, hs)
		}
		snaps = append(snaps, s)
	}

	skillMax := map[string]int{} // hero class + ability -> highest level emitted
	p.OnEntity(func(e *manta.Entity, op manta.EntityOp) error {
		cn := e.GetClassName()
		if op&manta.EntityOpDeleted != 0 {
			// entity indices are recycled: forget per-entity caches
			k := e.GetIndex()
			delete(abilOwner, k)
			delete(cdPrev, k)
			delete(abilityLvl, k)
		}
		// cooldown starts (casts) of hero abilities and items: [t, "cd", hero class, "", name, cooldown]
		if strings.HasPrefix(cn, "CDOTA_Ability_") || strings.HasPrefix(cn, "CDOTABaseAbility") || strings.HasPrefix(cn, "CDOTA_Item") {
			if cd, ok := getF(e, "m_fCooldown"); ok {
				key := e.GetIndex()
				prev := cdPrev[key]
				cdPrev[key] = cd
				if cd > prev+1 {
					var owner *manta.Entity
					if o := ownerOfAbility(key); o >= 0 {
						owner = heroes[o]
					}
					if owner != nil {
						if t, ok := gameTime(); ok {
							events = append(events, []any{math.Round(t*10) / 10, "cd", owner.GetClassName(), "", entName(e), math.Round(cd*10) / 10})
						}
					}
				}
			}
		}
		switch {
		case cn == "CDOTAGamerulesProxy":
			rules = e
		case cn == "CDOTA_PlayerResource":
			pres = e
		case cn == "CDOTA_DataRadiant":
			dataTeam[2] = e
		case cn == "CDOTA_DataDire":
			dataTeam[3] = e
		case strings.HasPrefix(cn, "CDOTA_Unit_Hero_"):
			if op&manta.EntityOpDeleted != 0 {
				delete(heroes, e.GetIndex())
			} else {
				heroes[e.GetIndex()] = e
			}
		case strings.HasPrefix(cn, "CDOTA_Ability_") || strings.HasPrefix(cn, "CDOTABaseAbility"):
			lvl, ok := getF(e, "m_iLevel")
			if !ok || lvl <= 0 {
				return nil
			}
			key := e.GetIndex()
			if abilityLvl[key] == int(lvl) {
				return nil
			}
			abilityLvl[key] = int(lvl)
			o := ownerOfAbility(key)
			if o < 0 {
				delete(abilityLvl, key) // owner not resolvable yet; retry on the next update
				return nil
			}
			he := heroes[o]
			if he == nil { // owner is not a tracked hero (e.g. a summon or a just-deleted hero entity)
				delete(abilityLvl, key)
				return nil
			}
			an := entName(e)
			if skillMax[he.GetClassName()+"/"+an] >= int(lvl) {
				return nil
			}
			skillMax[he.GetClassName()+"/"+an] = int(lvl)
			t, _ := gameTime()
			skills = append(skills, Skill{T: math.Round(t*10) / 10, Hero: he.GetClassName(), Ability: an, Level: int(lvl)})
		case cn == "CDOTA_BaseNPC_Creep_Lane" || cn == "CDOTA_BaseNPC_Creep_Siege":
			key := e.GetIndex()
			if op&manta.EntityOpDeleted != 0 {
				delete(creeps, key)
				delete(creepDead, key)
				return nil
			}
			creeps[key] = e
			if life, _ := getF(e, "m_lifeState"); life != 0 && !creepDead[key] {
				creepDead[key] = true
				if t, ok := gameTime(); ok && t <= creepUntil {
					x, y, _ := pos(e)
					team, _ := getF(e, "m_iTeamNum")
					creepDeaths.T = append(creepDeaths.T, int(math.Round(t*10)))
					creepDeaths.Team = append(creepDeaths.Team, int8(team))
					creepDeaths.X = append(creepDeaths.X, float32(math.Round(float64(x))))
					creepDeaths.Y = append(creepDeaths.Y, float32(math.Round(float64(y))))
				}
			}
			return nil
		case cn == "CDOTA_BaseNPC_Tower":
			if op&manta.EntityOpDeleted != 0 {
				delete(towers, e.GetIndex())
			} else {
				towers[e.GetIndex()] = e
			}
			return nil
		case cn == "CDOTA_NPC_Observer_Ward" || cn == "CDOTA_NPC_Observer_Ward_TrueSight":
			if cn == "CDOTA_NPC_Observer_Ward" {
				if op&manta.EntityOpDeleted != 0 {
					delete(obsEnt, e.GetIndex())
				} else {
					obsEnt[e.GetIndex()] = e
				}
			}
			t, _ := gameTime()
			if op&manta.EntityOpCreated != 0 {
				x, y, _ := pos(e)
				team, _ := getF(e, "m_iTeamNum")
				typ := "observer"
				if strings.HasSuffix(cn, "TrueSight") {
					typ = "sentry"
				}
				w := &Ward{Type: typ, Team: int(team), X: x, Y: y, Placed: t, Removed: -1, Owner: -1}
				wards[e.GetIndex()] = w
				wardList = append(wardList, w)
			}
			if op&manta.EntityOpDeleted != 0 {
				if w, ok := wards[e.GetIndex()]; ok {
					w.Removed = t
					delete(wards, e.GetIndex())
				}
			}
		}
		return nil
	})

	resolvePlayers := func() {
		if pres == nil || len(players) == 10 {
			return
		}
		players = nil
		for i := 0; i < 24; i++ {
			pd := fmt.Sprintf("m_vecPlayerData.%04d.", i)
			td := fmt.Sprintf("m_vecPlayerTeamData.%04d.", i)
			team, ok := getF(pres, pd+"m_iPlayerTeam")
			if !ok || (team != 2 && team != 3) {
				continue
			}
			hid, _ := getF(pres, td+"m_nSelectedHeroID")
			h, _ := getF(pres, td+"m_hSelectedHero")
			if hid == 0 || h == 0 || uint64(h) == 16777215 {
				return // heroes not all picked yet
			}
			var sid uint64
			switch v := pres.Get(pd + "m_iPlayerSteamID").(type) {
			case uint64:
				sid = v
			case int64:
				sid = uint64(v)
			}
			name, _ := pres.Get(pd + "m_iszPlayerName").(string)
			ent := int32(uint64(h) & 0x3FFF)
			ts, _ := getF(pres, td+"m_iTeamSlot")
			pl := &Player{Idx: len(players), Name: name, AccountID: int64(sid - steamBase), Team: int(team), HeroID: int(hid), TeamSlot: int(ts), entIdx: ent, pr: i}
			if sid == 0 {
				pl.AccountID = 0
			}
			if he, ok := heroes[ent]; ok {
				pl.HeroClass = he.GetClassName()
			}
			players = append(players, pl)
			tracks[pl.Idx] = &Track{}
		}
	}

	name := func(i uint32) string {
		v, _ := p.LookupStringByIndex("CombatLogNames", int32(i))
		return strings.TrimPrefix(v, "npc_dota_hero_")
	}
	p.Callbacks.OnCMsgDOTACombatLogEntry(func(m *dota.CMsgDOTACombatLogEntry) error {
		t, ok := gameTime()
		if !ok {
			return nil
		}
		t = math.Round(t*10) / 10
		switch m.GetType() {
		case dota.DOTA_COMBATLOG_TYPES_DOTA_COMBATLOG_DEATH:
			if !m.GetIsTargetHero() {
				// buildings and Roshan: [t, "obj", killer, target, "", target team]
				tn := name(m.GetTargetName())
				if strings.Contains(tn, "tower") || strings.Contains(tn, "rax") || strings.Contains(tn, "fort") ||
					strings.Contains(tn, "roshan") || strings.Contains(tn, "tormentor") {
					events = append(events, []any{t, "obj", name(m.GetAttackerName()), strings.TrimPrefix(tn, "npc_dota_"), "", m.GetTargetTeam()})
				}
				return nil
			}
			if m.GetIsTargetIllusion() {
				return nil
			}
			victim, _ := p.LookupStringByIndex("CombatLogNames", int32(m.GetTargetName()))
			killer, _ := p.LookupStringByIndex("CombatLogNames", int32(m.GetAttackerName()))
			d := Death{T: t, Victim: victim, Killer: killer}
			for _, pl := range players {
				if he, ok := heroes[pl.entIdx]; ok && norm(he.GetClassName()) == norm(victim) {
					d.X, d.Y, _ = pos(he)
				}
			}
			deaths = append(deaths, d)
			snapshot(t, "death:"+strings.TrimPrefix(victim, "npc_dota_hero_"))
		case dota.DOTA_COMBATLOG_TYPES_DOTA_COMBATLOG_DAMAGE:
			// hero (or hero-owned unit) -> enemy hero damage
			if !m.GetIsTargetHero() || m.GetIsTargetIllusion() || m.GetAttackerTeam() == m.GetTargetTeam() {
				return nil
			}
			src := name(m.GetDamageSourceName())
			if src == "" {
				src = name(m.GetAttackerName())
			}
			events = append(events, []any{t, "dmg", src, name(m.GetTargetName()), name(m.GetInflictorName()), m.GetValue()})
		case dota.DOTA_COMBATLOG_TYPES_DOTA_COMBATLOG_ABILITY:
			if !m.GetIsAttackerHero() || m.GetIsAttackerIllusion() || m.GetIsAbilityToggleOff() {
				return nil
			}
			events = append(events, []any{t, "cast", name(m.GetAttackerName()), name(m.GetTargetName()), name(m.GetInflictorName()), m.GetAbilityLevel()})
		case dota.DOTA_COMBATLOG_TYPES_DOTA_COMBATLOG_ITEM:
			if !m.GetIsAttackerHero() || m.GetIsAttackerIllusion() {
				return nil
			}
			events = append(events, []any{t, "item", name(m.GetAttackerName()), name(m.GetTargetName()), name(m.GetInflictorName()), 0})
		case dota.DOTA_COMBATLOG_TYPES_DOTA_COMBATLOG_MODIFIER_ADD:
			// debuffs applied by a hero to an enemy hero (stuns, slows, silences...)
			if !m.GetIsTargetHero() || m.GetIsTargetIllusion() || m.GetAttackerTeam() == m.GetTargetTeam() || m.GetAuraModifier() {
				return nil
			}
			src := name(m.GetAttackerName())
			events = append(events, []any{t, "debuff", src, name(m.GetTargetName()), name(m.GetInflictorName()), math.Round(float64(m.GetModifierDuration())*10) / 10})
		case dota.DOTA_COMBATLOG_TYPES_DOTA_COMBATLOG_PURCHASE:
			events = append(events, []any{t, "buy", name(m.GetTargetName()), "", strings.TrimPrefix(name(m.GetValue()), "item_"), 0})
		case dota.DOTA_COMBATLOG_TYPES_DOTA_COMBATLOG_PICKUP_RUNE:
			// [t, "rune", hero, "", rune type id, 0]
			events = append(events, []any{t, "rune", name(m.GetAttackerName()), "", fmt.Sprint(m.GetValue()), 0})
		case dota.DOTA_COMBATLOG_TYPES_DOTA_COMBATLOG_BUYBACK:
			events = append(events, []any{t, "buyback", "", "", "", m.GetValue()})
		}
		return nil
	})

	p.Callbacks.OnCDOTAUserMsg_ChatEvent(func(m *dota.CDOTAUserMsg_ChatEvent) error {
		// rune pickups: [t, "rune", "player:<id>", "", rune type id, bottled 0/1]
		ty := m.GetType()
		if ty != dota.DOTA_CHAT_MESSAGE_CHAT_MESSAGE_RUNE_PICKUP && ty != dota.DOTA_CHAT_MESSAGE_CHAT_MESSAGE_RUNE_BOTTLE {
			return nil
		}
		t, ok := gameTime()
		if !ok {
			return nil
		}
		b := 0
		if ty == dota.DOTA_CHAT_MESSAGE_CHAT_MESSAGE_RUNE_BOTTLE {
			b = 1
		}
		events = append(events, []any{math.Round(t*10) / 10, "rune", fmt.Sprintf("player:%d", m.GetPlayerid_1()), "", fmt.Sprint(m.GetValue()), b})
		return nil
	})

	p.Callbacks.OnCNETMsg_Tick(func(m *dota.CNETMsg_Tick) error {
		serverTick = m.GetTick()
		t, ok := gameTime()
		if !ok {
			return nil
		}
		resolvePlayers()
		if len(players) != 10 {
			return nil
		}
		if st, _ := getF(rules, "m_pGameRules.m_nGameState"); st >= 6 {
			if endTime == 0 {
				endTime = t
			}
			return nil
		}
		if q := int(math.Floor(t * 4)); q != lastQ {
			lastQ = q
			for _, pl := range players {
				he, ok := heroes[pl.entIdx]
				if !ok {
					continue
				}
				x, y, _ := pos(he)
				hp, _ := getF(he, "m_iHealth")
				mhp, _ := getF(he, "m_iMaxHealth")
				life, _ := getF(he, "m_lifeState")
				hpp := -1.0
				if life == 0 && mhp > 0 {
					hpp = math.Round(100 * hp / mhp)
				}
				tr := tracks[pl.Idx]
				tr.Q = append(tr.Q, int32(q))
				tr.QX = append(tr.QX, float32(math.Round(float64(x))))
				tr.QY = append(tr.QY, float32(math.Round(float64(y))))
				tr.QHP = append(tr.QHP, int8(hpp))
			}
		}
		sec := int(math.Floor(t))
		if sec == lastSec {
			return nil
		}
		lastSec = sec
		// vision providers this second: [team, x, y, range]
		tod, _ := getF(rules, "m_pGameRules.m_iNetTimeOfDay")
		frac := tod / 65536
		night := frac < 0.25 || frac >= 0.75
		if v, _ := getF(rules, "m_pGameRules.m_bIsTemporaryNight"); v > 0 {
			night = true
		}
		if v, _ := getF(rules, "m_pGameRules.m_bIsNightstalkerNight"); v > 0 {
			night = true
		}
		if v, _ := getF(rules, "m_pGameRules.m_bIsTemporaryDay"); v > 0 {
			night = false
		}
		type prov struct{ team, x, y, r float64 }
		var provs []prov
		addProv := func(e *manta.Entity, def float64) {
			if life, _ := getF(e, "m_lifeState"); life != 0 {
				return
			}
			team, _ := getF(e, "m_iTeamNum")
			x, y, ok := pos(e)
			if !ok {
				return
			}
			r, ok := getF(e, "m_iDayTimeVisionRange")
			if night {
				r, ok = getF(e, "m_iNightTimeVisionRange")
			}
			if !ok || r <= 0 {
				r = def
			}
			provs = append(provs, prov{team, float64(x), float64(y), r})
		}
		for _, pl := range players {
			if he, ok := heroes[pl.entIdx]; ok {
				addProv(he, 1800)
			}
		}
		for k, c := range creeps {
			if creepDead[k] {
				continue
			}
			addProv(c, 750)
			if t <= creepUntil {
				x, y, _ := pos(c)
				team, _ := getF(c, "m_iTeamNum")
				creepSnap.T = append(creepSnap.T, sec)
				creepSnap.Team = append(creepSnap.Team, int8(team))
				creepSnap.X = append(creepSnap.X, float32(math.Round(float64(x))))
				creepSnap.Y = append(creepSnap.Y, float32(math.Round(float64(y))))
			}
		}
		for _, tw := range towers {
			addProv(tw, 1900)
		}
		for _, w := range obsEnt {
			addProv(w, 1600)
		}
		for _, pl := range players {
			he, ok := heroes[pl.entIdx]
			if !ok {
				continue
			}
			if pl.HeroClass == "" {
				pl.HeroClass = he.GetClassName()
			}
			x, y, _ := pos(he)
			life, _ := getF(he, "m_lifeState")
			hp, _ := getF(he, "m_iHealth")
			mhp, _ := getF(he, "m_iMaxHealth")
			mana, _ := getF(he, "m_flMana")
			lvl, _ := getF(he, "m_iCurrentLevel")
			if pres != nil {
				td := fmt.Sprintf("m_vecPlayerTeamData.%04d.", pl.pr)
				k, _ := getF(pres, td+"m_iKills")
				d, _ := getF(pres, td+"m_iDeaths")
				as, _ := getF(pres, td+"m_iAssists")
				pl.Kills, pl.Deaths, pl.Assists = int(k), int(d), int(as)
			}
			tr := tracks[pl.Idx]
			tr.T = append(tr.T, sec)
			tr.X = append(tr.X, float32(math.Round(float64(x))))
			tr.Y = append(tr.Y, float32(math.Round(float64(y))))
			tr.Alive = append(tr.Alive, life == 0)
			tr.HP = append(tr.HP, int32(hp))
			tr.MaxHP = append(tr.MaxHP, int32(mhp))
			tr.Mana = append(tr.Mana, float32(math.Round(mana)))
			tr.Level = append(tr.Level, int32(lvl))
			var gold, nw, lh, dn float64
			if dt, ok := dataTeam[pl.Team]; ok {
				pre := fmt.Sprintf("m_vecDataTeam.%04d.", pl.TeamSlot)
				g1, _ := getF(dt, pre+"m_iReliableGold")
				g2, _ := getF(dt, pre+"m_iUnreliableGold")
				gold = g1 + g2
				nw, _ = getF(dt, pre+"m_iNetWorth")
				lh, _ = getF(dt, pre+"m_iLastHitCount")
				dn, _ = getF(dt, pre+"m_iDenyCount")
				tg, _ := getF(dt, pre+"m_iTotalEarnedGold")
				tx, _ := getF(dt, pre+"m_iTotalEarnedXP")
				tw, _ := getF(dt, pre+"m_flTowerDamage")
				hd, _ := getF(dt, pre+"m_flHeroDamage")
				pl.Gold, pl.XP, pl.TowerDmg, pl.HeroDmg = int(tg), int(tx), int(tw), int(hd)
			}
			tr.Gold = append(tr.Gold, int32(gold))
			tr.NW = append(tr.NW, int32(nw))
			tr.LH = append(tr.LH, int32(lh))
			tr.DN = append(tr.DN, int32(dn))
			seen := false
			for _, pv := range provs {
				if int(pv.team) != pl.Team && (pv.x-float64(x))*(pv.x-float64(x))+(pv.y-float64(y))*(pv.y-float64(y)) <= pv.r*pv.r {
					seen = true
					break
				}
			}
			tr.Vis = append(tr.Vis, seen)
		}
		return nil
	})

	if err := p.Start(); err != nil {
		fmt.Fprintln(os.Stderr, "parse error:", err)
	}
	type outT struct {
		Meta        map[string]any `json:"meta"`
		Players     []*Player      `json:"players"`
		Track       map[int]*Track `json:"track"`
		Deaths      []Death        `json:"deaths"`
		Wards       []*Ward        `json:"wards"`
		Events      [][]any        `json:"events"`
		Skills      []Skill        `json:"skills"`
		States      []*Snapshot    `json:"states"`
		Creeps      any            `json:"creeps"`
		CreepDeaths any            `json:"creep_deaths"`
	}
	out := outT{
		Meta:    map[string]any{"version": 5, "creeps_until": creepUntil, "game_start_sim": gameStart, "end_time": endTime, "total_ticks": p.Tick, "file": os.Args[1]},
		Players: players, Track: tracks, Deaths: deaths, Wards: wardList, Events: events, Skills: skills,
		States: snaps, Creeps: creepSnap, CreepDeaths: creepDeaths,
	}
	enc := json.NewEncoder(os.Stdout)
	if err := enc.Encode(out); err != nil {
		panic(err)
	}
}

// CDOTA_Unit_Hero_AntiMage -> npc_dota_hero_antimage (combat log naming)
func norm(s string) string {
	s = strings.TrimPrefix(strings.TrimPrefix(s, "CDOTA_Unit_Hero_"), "npc_dota_hero_")
	return strings.ToLower(strings.ReplaceAll(s, "_", ""))
}
