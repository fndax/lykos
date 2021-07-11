from __future__ import annotations

from collections import defaultdict, Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List, Union, Set

import threading
import itertools
import random
import time
import math
import re

from src.containers import UserDict, UserSet, DefaultUserDict
from src.debug import handle_error
from src.decorators import COMMANDS, command
from src.gamestate import GameState, PregameState
from src.gamestate import set_gamemode
from src.functions import get_players, match_role
from src.warnings import decrement_stasis
from src.messages import messages
from src.events import Event, event_listener
from src.cats import Wolfchat, All
from src import config, channels, locks, trans, reaper, users

if TYPE_CHECKING:
    from src.users import User
    from src.dispatcher import MessageDispatcher
    from src.channels import Channel

WAIT_TOKENS = 0
WAIT_LAST = 0

LAST_START: UserDict[User, List[Union[datetime, int]]] = UserDict()
LAST_WAIT: UserDict[User, datetime] = UserDict()
START_VOTES: UserSet = UserSet()
CAN_START_TIME: int = 0
FORCE_ROLES: DefaultUserDict[UserSet] = DefaultUserDict(UserSet)
RESTART_TRIES: int = 0
MAX_RETRIES = 3 # constant: not a setting

@command("wait", playing=True, phases=("join",))
def wait(wrapper: MessageDispatcher, message: str):
    """Increase the wait time until !start can be used."""
    if wrapper.target is not channels.Main:
        return

    with locks.wait:
        global WAIT_TOKENS, WAIT_LAST, CAN_START_TIME
        wait_check_time = time.time()
        WAIT_TOKENS += (wait_check_time - WAIT_LAST) / config.Main.get("timers.wait.command.tokenbucket.refill")
        WAIT_LAST = wait_check_time

        WAIT_TOKENS = min(WAIT_TOKENS, config.Main.get("timers.wait.command.tokenbucket.maximum"))

        now = datetime.now()
        if ((LAST_WAIT and wrapper.source in LAST_WAIT and LAST_WAIT[wrapper.source] +
                timedelta(seconds=config.Main.get("ratelimits.wait")) > now) or WAIT_TOKENS < 1):
            wrapper.pm(messages["command_ratelimited"])
            return

        LAST_WAIT[wrapper.source] = now
        WAIT_TOKENS -= 1
        wait_amount = config.Main.get("timers.wait.command.amount")
        if not config.Main.get("timers.wait.enabled"):
            wait_amount = 0
        if now > CAN_START_TIME:
            CAN_START_TIME = now + timedelta(seconds=wait_amount)
        else:
            CAN_START_TIME += timedelta(seconds=wait_amount)
        wrapper.send(messages["wait_time_increase"].format(wrapper.source, wait_amount))

@command("fwait", flag="w", phases=("join",))
def fwait(wrapper: MessageDispatcher, message: str):
    """Force an increase (or decrease) in wait time. Can be used with a number of seconds to wait."""
    global CAN_START_TIME

    msg = re.split(" +", message.strip(), 1)[0]

    if msg and (msg.isdigit() or (msg[0] == "-" and msg[1:].isdigit())):
        extra = int(msg)
    else:
        extra = config.Main.get("timers.wait.command.amount")

    now = datetime.now()
    extra = max(-900, min(900, extra))

    if now > CAN_START_TIME:
        CAN_START_TIME = now + timedelta(seconds=extra)
    else:
        CAN_START_TIME += timedelta(seconds=extra)

    if extra >= 0:
        wrapper.send(messages["forced_wait_time_increase"].format(wrapper.source, abs(extra)))
    else:
        wrapper.send(messages["forced_wait_time_decrease"].format(wrapper.source, abs(extra)))

@command("start", phases=("none", "join"))
def start_cmd(wrapper: MessageDispatcher, message: str):
    """Start a game of Werewolf."""
    if wrapper.target is channels.Main:
        start(wrapper)

@command("fstart", flag="S", phases=("join",))
def fstart(wrapper: MessageDispatcher, message: str):
    """Force the game to start immediately."""
    channels.Main.send(messages["fstart_success"].format(wrapper.source))
    wrapper.target = channels.Main
    start(wrapper, forced=True)

@command("retract", phases=("day", "join"))
def retract(wrapper: MessageDispatcher, message: str):
    """Take back your vote during the day (for whom to lynch)."""
    var = wrapper.game_state
    if wrapper.source not in get_players(var) or wrapper.source in reaper.DISCONNECTED:
        return

    with locks.reaper, locks.join_timer:
        if var.PHASE == "join":
            if wrapper.source not in START_VOTES:
                wrapper.pm(messages["start_novote"])
            else:
                START_VOTES.discard(wrapper.source)
                wrapper.send(messages["start_retract"].format(wrapper.source))

                if not START_VOTES:
                    trans.TIMERS["start_votes"][0].cancel()
                    del trans.TIMERS["start_votes"]

@event_listener("del_player")
def on_del_player(evt: Event, var: GameState, player: User, all_roles: Set[str], death_triggers: bool):
    if var.PHASE == "join":
        for role in FORCE_ROLES:
            FORCE_ROLES[role].discard(player)
        with locks.join_timer:
            START_VOTES.discard(player)

            # Cancel the start vote timer if there are no votes left
            if not START_VOTES and "start_votes" in trans.TIMERS:
                trans.TIMERS["start_votes"][0].cancel()
                del trans.TIMERS["start_votes"]

def start(wrapper: MessageDispatcher, *, forced: bool = False, restart: str = ""):
    from src.trans import stop_game

    pregame_state: PregameState = wrapper.game_state

    if (not forced and LAST_START and wrapper.source in LAST_START and
            LAST_START[wrapper.source][0] + timedelta(seconds=config.Main.get("ratelimits.start")) >
            datetime.now() and not restart):
        LAST_START[wrapper.source][1] += 1
        wrapper.source.send(messages["command_ratelimited"])
        return

    global RESTART_TRIES
    if restart:
        RESTART_TRIES += 1
    if RESTART_TRIES > MAX_RETRIES:
        stop_game(pregame_state, abort=True, log=False)
        return

    if not restart:
        LAST_START[wrapper.source] = [datetime.now(), 1]

    if not restart:
        if pregame_state is None:
            wrapper.source.send(messages["no_game_running"])
            return
        if pregame_state.in_game:
            wrapper.source.send(messages["werewolf_already_running"])
            return

        villagers = get_players(pregame_state)
        vils = set(get_players(pregame_state))

        if wrapper.source not in villagers and not forced:
            return

        dur = int((CAN_START_TIME - datetime.now()).total_seconds())
        if dur > 0 and not forced:
            wrapper.send(messages["please_wait"].format(dur))
            return

        if len(villagers) < var.MIN_PLAYERS:
            wrapper.send(messages["not_enough_players"].format(wrapper.source, var.MIN_PLAYERS))
            return

        if len(villagers) > var.MAX_PLAYERS:
            wrapper.send.send(messages["max_players"].format(wrapper.source, var.MAX_PLAYERS))
            return

        with locks.join_timer:
            if not forced and wrapper.source in START_VOTES:
                wrapper.pm(messages["start_already_voted"])
                return

            start_votes_required = min(math.ceil(len(villagers) * var.START_VOTES_SCALE), var.START_VOTES_MAX)
            if not forced and len(START_VOTES) < start_votes_required:
                # If there's only one more vote required, start the game immediately.
                # Checked here to make sure that a player that has already voted can't
                # vote again for the final start.
                if len(START_VOTES) < start_votes_required - 1:
                    START_VOTES.add(wrapper.source)
                    remaining_votes = start_votes_required - len(START_VOTES)
                    wrapper.send(messages["start_voted"].format(wrapper.source, remaining_votes))

                    # If this was the first vote
                    if len(START_VOTES) == 1:
                        t = threading.Timer(60, expire_start_votes, (pregame_state, wrapper.target))
                        trans.TIMERS["start_votes"] = (t, time.time(), 60)
                        t.daemon = True
                        t.start()
                    return

        if not var.FGAMED:
            from src.gamemodes import GAME_MODES
            votes = {} #key = gamemode, not hostmask
            for gamemode in var.GAMEMODE_VOTES.values():
                if len(villagers) >= GAME_MODES[gamemode][1] and len(villagers) <= GAME_MODES[gamemode][2]:
                    votes[gamemode] = votes.get(gamemode, 0) + 1
            voted = [gamemode for gamemode in votes if votes[gamemode] == max(votes.values()) and votes[gamemode] >= len(villagers)/2]
            if voted:
                set_gamemode(pregame_state, random.choice(voted))
            else:
                possiblegamemodes = []
                numvotes = 0
                for gamemode, num in votes.items():
                    if len(villagers) < GAME_MODES[gamemode][1] or len(villagers) > GAME_MODES[gamemode][2] or GAME_MODES[gamemode][3] == 0:
                        continue
                    possiblegamemodes += [gamemode] * num
                    numvotes += num
                if len(villagers) - numvotes > 0:
                    possiblegamemodes += [None] * ((len(villagers) - numvotes) // 2)
                # check if we go with a voted mode or a random mode
                gamemode = random.choice(possiblegamemodes)
                if gamemode is None:
                    possiblegamemodes = []
                    for gamemode in GAME_MODES.keys() - set(config.Main.get("gameplay.disable.gamemodes")):
                        if len(villagers) >= GAME_MODES[gamemode][1] and len(villagers) <= GAME_MODES[gamemode][2] and GAME_MODES[gamemode][3] > 0:
                            possiblegamemodes += [gamemode] * GAME_MODES[gamemode][3]
                    gamemode = random.choice(possiblegamemodes)
                set_gamemode(pregame_state, gamemode)

    else:
        set_gamemode(pregame_state, restart)
        var.GAME_ID = time.time() # restart reaper timer

    # Initial checks passed, game mode has been fully initialized
    # We move from pregame state to in-game state
    ingame_state = GameState(pregame_state)

    event = Event("role_attribution", {"addroles": Counter()})
    if event.dispatch(ingame_state, villagers):
        addroles = event.data["addroles"]
        strip = lambda x: re.sub(r"\(.*\)", "", x)
        lv = len(villagers)
        roles = []
        for num, rolelist in ingame_state.current_mode.ROLE_GUIDE.items():
            if num <= lv:
                roles.extend(rolelist)
        defroles = Counter(strip(x) for x in roles)
        for role, count in list(defroles.items()):
            if role[0] == "-":
                srole = role[1:]
                defroles[srole] -= count
                del defroles[role]
                if defroles[srole] == 0:
                    del defroles[srole]
        if not defroles:
            wrapper.send(messages["no_settings_defined"].format(wrapper.source, lv))
            return
        for role, num in defroles.items():
            # if an event defined this role, use that number. Otherwise use the number from ROLE_GUIDE
            addroles[role] = addroles.get(role, num)
        if sum([addroles[r] for r in addroles if r not in ingame_state.current_mode.SECONDARY_ROLES]) > lv:
            wrapper.send(messages["too_many_roles"])
            stop_game(ingame_state, abort=True, log=False)
            return
        for role in All:
            addroles.setdefault(role, 0)
    else:
        addroles = event.data["addroles"]

    # convert roleset aliases into the appropriate roles
    possible_rolesets = [Counter()]
    roleset_roles = defaultdict(int)
    ingame_state.current_mode.ACTIVE_ROLE_SETS = {}
    for role, amt in list(addroles.items()):
        # not a roleset? add a fixed amount of them
        if role not in ingame_state.current_mode.ROLE_SETS:
            for pr in possible_rolesets:
                pr[role] += amt
            continue
        # if a roleset, ensure we don't try to expose the roleset name in !stats or future attribution
        # but do keep track of the sets in use so we can have !stats reflect proper information
        ingame_state.current_mode.ACTIVE_ROLE_SETS[role] = amt
        del addroles[role]
        # init !stats with all 0s so that it can number things properly; the keys need to exist in the Counter
        # across every possible roleset so that !stats works right
        rs = Counter(ingame_state.current_mode.ROLE_SETS[role])
        for r in rs:
            for pr in possible_rolesets:
                pr[r] += 0
        toadd = random.sample(list(rs.elements()), amt)
        for r in toadd:
            addroles[r] += 1
            roleset_roles[r] += 1
        add_rolesets = []
        temp_rolesets = []
        for c in itertools.combinations(rs.elements(), amt):
            add_rolesets.append(Counter(c))
        for pr in possible_rolesets:
            for ar in add_rolesets:
                temp = Counter(pr)
                temp.update(ar)
                temp_rolesets.append(temp)
        possible_rolesets = temp_rolesets

    if ingame_state.current_mode.CUSTOM_SETTINGS._overridden and not restart:  # Custom settings
        need_reset = True
        wvs = sum(addroles[r] for r in Wolfchat)
        if len(villagers) < (sum(addroles.values()) - sum(addroles[r] for r in ingame_state.current_mode.SECONDARY_ROLES)):
            wrapper.send(messages["too_few_players_custom"])
        elif not wvs:
            wrapper.send(messages["need_one_wolf"])
        elif wvs > (len(villagers) / 2):
            wrapper.send(messages["too_many_wolves"])
        else:
            need_reset = False

        if need_reset:
            wrapper.send(messages["default_reset"])
            stop_game(ingame_state, abort=True, log=False)
            return

    if var.ADMIN_TO_PING is not None and not restart:
        for decor in (COMMANDS["join"] + COMMANDS["start"]):
            decor(_command_disabled)

    var.MAIN_ROLES.clear()
    var.NIGHT_COUNT = 0
    var.DAY_COUNT = 0
    var.FINAL_ROLES.clear()

    var.DEADCHAT_PLAYERS.clear()
    var.SPECTATING_WOLFCHAT.clear()
    var.SPECTATING_DEADCHAT.clear()

    # Second round of check is done: Initialize the various variables that we need
    ingame_state.begin_setup()

    # handle forced main roles
    for role, count in addroles.items():
        if role in ingame_state.current_mode.SECONDARY_ROLES or role not in FORCE_ROLES:
            continue
        to_add = set()
        force_roles = list(FORCE_ROLES[role])
        random.shuffle(force_roles)
        for user in force_roles:
            # If we already assigned enough people to this role, ignore the rest
            if count == 0:
                break
            # If multiple main roles were forced, only honor one of them
            if user in var.MAIN_ROLES:
                continue
            ingame_state.roles[role].add(user)
            var.MAIN_ROLES[user] = role
            var.ORIGINAL_MAIN_ROLES[user] = role
            vils.remove(user)
            to_add.add(user)
            count -= 1

        # update count for next loop that performs regular (non-forced) role assignment
        addroles[role] = count

    # handle regular role assignment
    for role, count in addroles.items():
        # Check if we already assigned enough people to this role above
        # or if it's a secondary role (those are assigned later on)
        if count == 0 or role in ingame_state.current_mode.SECONDARY_ROLES:
            continue

        selected = random.sample(vils, count)
        for x in selected:
            var.MAIN_ROLES[x] = role
            var.ORIGINAL_MAIN_ROLES[x] = role
            vils.remove(x)
        ingame_state.roles[role].update(selected)
        addroles[role] = 0

    ingame_state.roles[ingame_state.default_role].update(vils)
    for x in vils:
        var.MAIN_ROLES[x] = ingame_state.default_role
        var.ORIGINAL_MAIN_ROLES[x] = ingame_state.default_role
    if vils:
        for pr in possible_rolesets:
            pr[ingame_state.default_role] += len(vils)

    # Collapse possible_rolesets into global role stats
    # which is a FrozenSet[FrozenSet[Tuple[str, int]]]
    possible_rolesets_set = set()
    event = Event("reconfigure_stats", {"new": []})
    for pr in possible_rolesets:
        event.data["new"] = [pr]
        event.dispatch(ingame_state, pr, "start")
        for v in event.data["new"]:
            if min(v.values()) >= 0:
                possible_rolesets_set.add(frozenset(v.items()))
    ingame_state.set_role_stats(possible_rolesets_set)

    # Now for the secondary roles
    for role, dfn in ingame_state.current_mode.SECONDARY_ROLES.items():
        count = addroles[role]
        possible = get_players(ingame_state, dfn)
        if role in FORCE_ROLES:
            force_roles = list(FORCE_ROLES[role])
            random.shuffle(force_roles)
            for user in force_roles:
                if count == 0:
                    break
                if user in possible:
                    ingame_state.roles[role].add(user)
                    possible.remove(user)
                    count -= 1
        # Don't do anything further if this secondary role was forced on enough players already
        if count == 0:
            continue
        if len(possible) < count:
            wrapper.send(messages["not_enough_targets"].format(role))
            stop_game(ingame_state, abort=True, log=False)
            return
        ingame_state.roles[role].update(x for x in random.sample(possible, count))

    # Give game modes the ability to customize who was assigned which role after everything's been set
    # The listener can add the following tuples into the "actions" dict to specify modifications
    # Directly modifying var.MAIN_ROLES, var.roles, etc. is **NOT SUPPORTED**
    # ("swap", User, User) -- swaps the main role of the two given users
    # ("add", User, str) -- adds a secondary role to the user (no-op if user already has that role)
    # ("remove", User, str) -- removes a secondary role from the user (no-op if it's not a secondary role for user)
    # Actions are applied in order
    event = Event("role_attribution_end", {"actions": []})
    event.dispatch(ingame_state, var.MAIN_ROLES, ingame_state.roles)
    for tup in event.data["actions"]:
        if tup[0] == "swap":
            if tup[1] not in var.MAIN_ROLES or tup[2] not in var.MAIN_ROLES:
                raise KeyError("Users in role_attribution_end:swap action must be playing")
            onerole = var.MAIN_ROLES[tup[1]]
            tworole = var.MAIN_ROLES[tup[2]]
            var.MAIN_ROLES[tup[1]] = tworole
            var.ORIGINAL_MAIN_ROLES[tup[1]] = tworole
            ingame_state.roles[onerole].discard(tup[1])
            ingame_state.roles[tworole].add(tup[1])

            var.MAIN_ROLES[tup[2]] = onerole
            var.ORIGINAL_MAIN_ROLES[tup[2]] = onerole
            ingame_state.roles[tworole].discard(tup[2])
            ingame_state.roles[onerole].add(tup[2])
        elif tup[0] == "add":
            if tup[1] not in var.MAIN_ROLES or tup[2] not in All:
                raise KeyError("Invalid user or role in role_attribution_end:add action")
            ingame_state.roles[tup[2]].add(tup[1])
        elif tup[0] == "remove":
            if tup[1] not in var.MAIN_ROLES or tup[2] not in All:
                raise KeyError("Invalid user or role in role_attribution_end:remove action")
            if var.MAIN_ROLES[tup[1]] == tup[2]:
                raise ValueError("Cannot remove a user's main role in role_attribution_end:remove action")
            ingame_state.roles[tup[2]].discard(tup[1])
        else:
            raise KeyError("Invalid action for role_attribution_end")

    with locks.join_timer: # cancel timers
        for name in ("join", "join_pinger", "start_votes"):
            if name in trans.TIMERS:
                trans.TIMERS[name][0].cancel()
                del trans.TIMERS[name]

    var.LAST_STATS = None
    var.LAST_TIME = None

    for role, players in ingame_state.roles.items():
        for player in players:
            evt = Event("new_role", {"messages": [], "role": role, "in_wolfchat": False}, inherit_from=None)
            evt.dispatch(ingame_state, player, None)

    start_event = Event("start_game", {"custom_game_callback": None})  # defined here to make the linter happy
    if not restart:
        gamemode = ingame_state.current_mode.name
        start_event.dispatch(ingame_state, gamemode, ingame_state.current_mode)

        # Alert the players to option changes they may not be aware of
        # All keys begin with gso_* (game start options)
        options = []
        if ingame_state.current_mode.CUSTOM_SETTINGS._role_reveal is not None:
            # Keys used here: gso_rr_on, gso_rr_team, gso_rr_off
            options.append(messages["gso_rr_{0}".format(ingame_state.role_reveal)])
        if ingame_state.current_mode.CUSTOM_SETTINGS._stats_type is not None:
            # Keys used here: gso_st_default, gso_st_accurate, gso_st_team, gso_st_disabled
            options.append(messages["gso_st_{0}".format(ingame_state.stats_type)])
        if ingame_state.current_mode.CUSTOM_SETTINGS._abstain_enabled is not None or ingame_state.current_mode.CUSTOM_SETTINGS._limit_abstain is not None:
            if ingame_state.abstain_enabled and ingame_state.limit_abstain:
                options.append(messages["gso_abs_rest"])
            elif ingame_state.abstain_enabled:
                options.append(messages["gso_abs_unrest"])
            else:
                options.append(messages["gso_abs_none"])

        key = "welcome_simple"
        if options:
            key = "welcome_options"
        wrapper.send(messages[key].format(villagers, gamemode, options))
        wrapper.target.mode("+m")

    var.ORIGINAL_ROLES.clear()
    for role, players in ingame_state.ROLES.items():
        var.ORIGINAL_ROLES[role] = players.copy()

    var.DAY_TIMEDELTA = timedelta(0)
    var.NIGHT_TIMEDELTA = timedelta(0)
    var.LAST_PING = None

    if restart:
        var.PHASE = "join" # allow transition_* to run properly if game was restarted on first night
    if not ingame_state.start_with_day:
        from src.trans import transition_night
        var.GAMEPHASE = "day" # gamephase needs to be the thing we're transitioning from
        transition_night(ingame_state)
    elif start_event.data["custom_game_callback"]:
        start_event.data["custom_game_callback"](ingame_state)
    else:
        # send role messages
        evt = Event("send_role", {})
        evt.dispatch(ingame_state)
        from src.trans import transition_day
        var.GAMEPHASE = "night"
        transition_day(ingame_state)

    decrement_stasis()

    # Game is starting, finalize setup
    ingame_state.finish_setup()

    if config.Main.get("reaper.enabled"):
        # DEATH TO IDLERS!
        from src.reaper import reaper
        reapertimer = threading.Thread(None, reaper, args=(ingame_state, var.GAME_ID))
        reapertimer.daemon = True
        reapertimer.start()

def _command_disabled(wrapper: MessageDispatcher, message: str):
    wrapper.send(messages["command_disabled_admin"])

@handle_error
def expire_start_votes(var: GameState, channel: Channel):
    # Should never happen as the timer is removed on game start, but just to be safe
    if var.PHASE != "join":
        return

    with locks.join_timer:
        START_VOTES.clear()
        channel.send(messages["start_expired"])

@command("frole", flag="d", phases=("join",))
def frole(wrapper: MessageDispatcher, message: str):
    """Force a player into a certain role."""
    pl = get_players(wrapper.game_state)

    parts = message.lower().split(",")
    for part in parts:
        try:
            (name, role) = part.split(":", 1)
        except ValueError:
            wrapper.send(messages["frole_incorrect"].format(part))
            return
        umatch = users.complete_match(name.strip(), pl)
        rmatch = match_role(role.strip(), allow_special=False)
        role = None
        if rmatch:
            role = rmatch.get().key
        if not umatch or not rmatch or role == wrapper.game_state.default_role:
            wrapper.send(messages["frole_incorrect"].format(part))
            return
        FORCE_ROLES[role].add(umatch.get())

    wrapper.send(messages["operation_successful"])

@event_listener("reset")
def on_reset(evt: Event, var: GameState):
    global MAX_RETRIES, WAIT_TOKENS, WAIT_LAST, CAN_START_TIME
    LAST_START.clear()
    LAST_WAIT.clear()
    START_VOTES.clear()
    FORCE_ROLES.clear()
    MAX_RETRIES = 0
    WAIT_TOKENS = 0
    WAIT_LAST = 0
    CAN_START_TIME = 0
