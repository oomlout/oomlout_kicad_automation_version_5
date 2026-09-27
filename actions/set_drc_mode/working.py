"""Select light/normal/tight with optional independent mask-check switches."""
from ...authoring import set_drc_mode, high_level_action
from ...command_api import dispatch_command
from ...expansion import expand_high_level_action
from ...debug import traced

ACTION_NAME = "set_drc_mode"

@traced(scope="actions.set_drc_mode.main")
def main(*, board, command):
    return dispatch_command(operation=set_drc_mode, board=board, command=command, required_keys={"mode"})

@traced(scope="actions.set_drc_mode.get_high_level_action")
def get_high_level_action(**kwargs):
    return high_level_action(ACTION_NAME, **kwargs)

@traced(scope="actions.set_drc_mode.expand_action")
def expand_action(*, action, **kwargs):
    return expand_high_level_action(action=action, **kwargs)
