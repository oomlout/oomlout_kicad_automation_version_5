"""Declare a logical placement block."""

from ...authoring import add_block, high_level_action
from ...command_api import dispatch_command
from ...expansion import expand_high_level_action
from ...debug import traced

ACTION_NAME = "add_block"


@traced(scope="actions.add_block.main")
def main(*, board, command):
    return dispatch_command(
        operation=add_block,
        board=board,
        command=command,
        required_keys={"name"},
    )


@traced(scope="actions.add_block.get_high_level_action")
def get_high_level_action(**kwargs):
    return high_level_action(ACTION_NAME, **kwargs)


@traced(scope="actions.add_block.expand_action")
def expand_action(*, action, **kwargs):
    return expand_high_level_action(action=action, **kwargs)
