"""Declare one intentional no-connect endpoint."""

from ...authoring import high_level_action, no_connect
from ...command_api import dispatch_command
from ...expansion import expand_high_level_action
from ...debug import traced

ACTION_NAME = "no_connect"


@traced(scope="actions.no_connect.main")
def main(*, board, command):
    return dispatch_command(
        operation=no_connect,
        board=board,
        command=command,
        required_keys={"part", "pin"},
    )


@traced(scope="actions.no_connect.get_high_level_action")
def get_high_level_action(**kwargs):
    return high_level_action(ACTION_NAME, **kwargs)


@traced(scope="actions.no_connect.expand_action")
def expand_action(*, action, **kwargs):
    return expand_high_level_action(action=action, **kwargs)
