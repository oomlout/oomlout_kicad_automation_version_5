"""Declare OOBB board geometry and its four mechanical holes."""

from ...authoring import board_oobb, high_level_action
from ...command_api import dispatch_command
from ...expansion import expand_high_level_action
from ...debug import traced

ACTION_NAME = "board_oobb"


@traced(scope="actions.board_oobb.main")
def main(*, board, command):
    return dispatch_command(
        operation=board_oobb,
        board=board,
        command=command,
        required_keys={"width_units", "height_units"},
    )


@traced(scope="actions.board_oobb.get_high_level_action")
def get_high_level_action(**kwargs):
    return high_level_action(ACTION_NAME, **kwargs)


@traced(scope="actions.board_oobb.expand_action")
def expand_action(*, action, **kwargs):
    return expand_high_level_action(action=action, **kwargs)
