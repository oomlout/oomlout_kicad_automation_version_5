"""Declare a pairwise electrical connection."""

from ...authoring import connect, high_level_action
from ...command_api import connection_command, dispatch_command
from ...expansion import expand_high_level_action
from ...debug import traced

ACTION_NAME = "connect"


@traced(scope="actions.connect.main")
def main(*, board, command):
    return dispatch_command(
        operation=connect,
        board=board,
        command=connection_command(command),
        required_keys={"part_a", "pin_a", "part_b", "pin_b"},
    )


@traced(scope="actions.connect.get_high_level_action")
def get_high_level_action(**kwargs):
    return high_level_action(ACTION_NAME, **kwargs)


@traced(scope="actions.connect.expand_action")
def expand_action(*, action, **kwargs):
    return expand_high_level_action(action=action, **kwargs)
