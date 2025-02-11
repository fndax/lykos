from __future__ import annotations

import sys
from typing import Optional

from src import channels, trans, config
from src.cats import All, Wolf
from src.events import Event, event_listener
from src.gamestate import GameState
from src.messages import messages
from src.roles.helper.wolves import register_wolf, get_wolfchat_roles
from src.status import in_misdirection_scope
from src.users import User

register_wolf("traitor")

@event_listener("get_reveal_role")
def on_get_reveal_role(evt: Event, var: GameState, user: User):
    # in team reveal, show traitor as wolfteam, otherwise team stats won't sync with how
    # they're revealed upon death. Team stats should show traitor as wolfteam or else
    # the stats are wrong in that they'll report one less wolf than actually exists,
    # which can confuse a lot of people
    if evt.data["role"] == "traitor" and config.Main.get("gameplay.hidden.traitor") and var.role_reveal != "team":
        evt.data["role"] = var.hidden_role

@event_listener("get_final_role")
def on_get_final_role(evt: Event, var: GameState, user: User, role: str):
    # if a traitor turns we want to show them as traitor in the end game readout
    # instead of "wolf (was traitor)"
    if role == "traitor" and evt.data["role"] == "wolf":
        evt.data["role"] = "traitor"

@event_listener("update_stats", priority=1)
def on_update_stats1(evt: Event, var: GameState, player: User, mainrole: str, revealrole: str, allroles: set[str]):
    if mainrole == var.hidden_role and config.Main.get("gameplay.hidden.traitor"):
        evt.data["possible"].add("traitor")

@event_listener("update_stats", priority=3)
def on_update_stats3(evt: Event, var: GameState, player: User, mainrole: str, revealrole: str, allroles: set[str]):
    # if this is a night death and we know for sure that wolves (and only wolves)
    # killed, then that kill cannot be traitor as long as they're in wolfchat.
    wolfchat = get_wolfchat_roles()
    if evt.params.reason != "night_death":
        # a chained death, someone dying during day, or someone idling out
        # either way, traitor can die here
        return
    if "traitor" not in wolfchat:
        # wolves can kill traitor normally in this configuration
        return
    if "traitor" not in evt.data["possible"]:
        # not under consideration
        return
    if mainrole == "traitor":
        # definitely dying, so we shouldn't remove them from consideration
        # this may lead to info leaks, but info leaks are better than !stats just entirely breaking
        return
    if in_misdirection_scope(var, Wolf, as_actor=True) or in_misdirection_scope(var, All - wolfchat, as_target=True):
        # luck/misdirection totems are in play, a wolf kill could have bounced to traitor anyway
        return

    if var.current_phase == "day" and var.in_phase_transition:
        mevt = Event("get_role_metadata", {})
        mevt.dispatch(var, "night_kills")
        nonwolf = 0
        total = 0
        for role, num in mevt.data.items():
            if role != "wolf":
                nonwolf += num
            total += num
        if nonwolf == 0:
            evt.data["possible"].discard("traitor")
            return
        # TODO: this doesn't account for everything, for example if there was a hunter kill
        # and a wolf kill, and a wolf + villager died, we know the villager was the wolf kill
        # and therefore cannot be traitor. However, we currently do not have the logic to deduce this

@event_listener("chk_win", priority=1.1)
def on_chk_win(evt: Event, var: GameState, rolemap: dict[str, set[User]], mainroles: dict[User, str], lpl: int, lwolves: int, lrealwolves: int):
    did_something = False
    if lrealwolves == 0:
        for traitor in list(rolemap["traitor"]):
            trans.NIGHT_IDLE_EXEMPT.add(traitor) # if they turn during night, don't give them idle warnings
            rolemap["wolf"].add(traitor)
            rolemap["traitor"].remove(traitor)
            if "cursed villager" in rolemap:
                rolemap["cursed villager"].discard(traitor)
            mainroles[traitor] = "wolf"
            did_something = True
            if var.in_game:
                var.final_roles[traitor] = "wolf"
                traitor.send(messages["traitor_turn"])
    if did_something:
        if var.in_game:
            channels.Main.send(messages["traitor_turn_channel"])
            # fix !stats to show that traitor turned as well
            newstats = set()
            for rs in var.get_role_stats():
                d = dict(rs)
                # traitor count of 0 is not possible since we for-sure turned traitors into wolves earlier
                # as such, exclude such cases from newstats entirely.
                if d["traitor"] >= 1:
                    d["wolf"] = d.get("wolf", 0) + d["traitor"]
                    d["traitor"] = 0
                    newstats.add(frozenset(d.items()))
                # if amnesiac is loaded and they have turned, there may be extra traitors not normally accounted for
                if "src.roles.amnesiac" in sys.modules:
                    from src.roles.amnesiac import get_blacklist, get_stats_flag
                    if get_stats_flag(var) and "traitor" not in get_blacklist(var) and d["amnesiac"] >= 1:
                        iter_end = d["amnesiac"] + 1
                        for i in range(1, iter_end):
                            d["wolf"] = d.get("wolf", 0) + 1
                            d["amnesiac"] -= 1
                            newstats.add(frozenset(d.items()))

            var.set_role_stats(newstats)

        evt.prevent_default = True
        evt.stop_processing = True

@event_listener("get_role_metadata")
def on_get_role_metadata(evt: Event, var: Optional[GameState], kind: str):
    if kind == "role_categories":
        evt.data["traitor"] = {"Wolfchat", "Wolfteam", "Wolf Objective"}
