from __future__ import annotations

import re
import random
import itertools
import math
from collections import defaultdict
from typing import Optional, TYPE_CHECKING

from src import channels, users
from src.functions import get_players, get_all_players, get_main_role, get_reveal_role, get_all_roles, get_target, change_role
from src.decorators import command
from src.containers import UserList, UserSet, UserDict, DefaultUserDict
from src.messages import messages
from src.status import try_misdirection, try_exchange
from src.events import Event, event_listener

from src.roles.helper.wolves import get_wolfchat_roles

if TYPE_CHECKING:
    from src.users import User
    from src.dispatcher import MessageDispatcher
    from src.gamestate import GameState

IDOLS: UserDict[User, User] = UserDict()
CAN_ACT = UserSet()
ACTED = UserSet()

@command("choose", chan=False, pm=True, playing=True, phases=("night",), roles=("wild child",))
def choose_idol(wrapper: MessageDispatcher, message: str):
    """Pick your idol, if they die, you'll become a wolf!"""
    if wrapper.source in IDOLS:
        wrapper.pm(messages["wild_child_already_picked"])
        return

    idol = get_target(wrapper, re.split(" +", message)[0])
    if not idol:
        return

    IDOLS[wrapper.source] = idol
    ACTED.add(wrapper.source)
    wrapper.send(messages["wild_child_success"].format(idol))

@event_listener("see")
def on_see(evt: Event, var: GameState, seer: User, target: User):
    if target in get_all_players(var, ("wild child",)):
        evt.data["role"] = "wild child"

@event_listener("new_role")
def on_new_role(evt: Event, var: GameState, player: User, old_role: Optional[str]):
    if evt.data["role"] == "wolf" and old_role == "wild child" and evt.params.inherit_from and "wild child" in get_all_roles(var, evt.params.inherit_from):
        evt.data["role"] = "wild child"

    if evt.params.inherit_from in IDOLS and "wild child" not in get_all_roles(var, player):
        IDOLS[player] = IDOLS.pop(evt.params.inherit_from)
        evt.data["messages"].append(messages["wild_child_idol"].format(IDOLS[player]))

@event_listener("swap_role_state")
def on_swap_role_state(evt: Event, var: GameState, actor: User, target: User, role: str):
    if role == "wild child":
        IDOLS[actor], IDOLS[target] = IDOLS[target], IDOLS[actor]
        if IDOLS[actor] in get_players(var):
            evt.data["actor_messages"].append(messages["wild_child_idol"].format(IDOLS[actor]))
        else: # The King is dead, long live the King!
            change_role(var, actor, "wild child", "wolf", message="wild_child_idol_died")
            var.roles["wild child"].add(actor)

        if IDOLS[target] in get_players(var):
            evt.data["target_messages"].append(messages["wild_child_idol"].format(IDOLS[target]))
        else:
            change_role(var, target, "wild child", "wolf", message="wild_child_idol_died")
            var.roles["wild child"].add(target)

@event_listener("myrole")
def on_myrole(evt: Event, var: GameState, user: User):
    if user in IDOLS and user not in get_players(var, get_wolfchat_roles()):
        evt.data["messages"].append(messages["wild_child_idol"].format(IDOLS[user]))

@event_listener("del_player")
def on_del_player(evt: Event, var: GameState, player: User, all_roles: set[str], death_triggers: bool):
    del IDOLS[:player:]
    CAN_ACT.discard(player)
    ACTED.discard(player)
    if not death_triggers:
        return

    for child in get_all_players(var, ("wild child",)):
        if IDOLS.get(child) is player:
            # Change their main role to wolf
            change_role(var, child, get_main_role(var, child), "wolf", message="wild_child_idol_died")
            var.roles["wild child"].add(child)

@event_listener("chk_nightdone")
def on_chk_nightdone(evt: Event, var: GameState):
    evt.data["acted"].extend(ACTED)
    evt.data["nightroles"].extend(CAN_ACT)

@event_listener("transition_day_begin")
def on_transition_day_begin(evt: Event, var: GameState):
    if not var.start_with_day or var.day_count > 0:
        for child in get_all_players(var, ("wild child",)):
            if child not in IDOLS:
                players = get_players(var)
                players.remove(child)
                if players:
                    idol = random.choice(players)
                    IDOLS[child] = idol
                    child.send(messages["wild_child_random_idol"].format(idol))
                    idol_role = get_main_role(var, idol)

@event_listener("send_role")
def on_transition_night_end(evt: Event, var: GameState):
    CAN_ACT.update(get_all_players(var, ("wild child",)) - IDOLS.keys())
    for child in get_all_players(var, ("wild child",)):
        if child not in IDOLS:
            pl = list(get_players(var))
            pl.remove(child)
            random.shuffle(pl)
            child.send(messages["wild_child_notify"])
            if var.next_phase != "night":
                child.send(messages["players_list"].format(pl))

@event_listener("revealroles_role")
def on_revealroles_role(evt: Event, var: GameState, user: User, role: str):
    if role == "wild child" and user not in get_players(var, get_wolfchat_roles()):
        if user in IDOLS:
            evt.data["special_case"].append(messages["wild_child_revealroles_picked"].format(IDOLS[user]))
        else:
            evt.data["special_case"].append(messages["wild_child_revealroles_no_idol"])

@event_listener("get_reveal_role")
def on_get_reveal_role(evt: Event, var: GameState, user: User):
    if evt.data["role"] == "wolf" and user in get_all_players(var, ("wild child",)):
        evt.data["role"] = "wild child"

@event_listener("update_stats")
def on_update_stats(evt: Event, var: GameState, player: User, main_role: str, reveal_role: str, all_roles: set[str]):
    if reveal_role == "wild child":
        # wild children always die as such even if their main_role is a wolf role
        evt.data["possible"] = {"wild child"}

@event_listener("begin_day")
def on_begin_day(evt: Event, var: GameState):
    CAN_ACT.clear()
    ACTED.clear()

@event_listener("reset")
def on_reset(evt: Event, var: GameState):
    IDOLS.clear()
    CAN_ACT.clear()
    ACTED.clear()

@event_listener("get_role_metadata")
def on_get_role_metadata(evt: Event, var: Optional[GameState], kind: str):
    if kind == "role_categories":
        evt.data["wild child"] = {"Village", "Team Switcher"}
