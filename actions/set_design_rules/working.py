"""Declare explicit project minimums and Default-netclass clearance in mm."""
from ...authoring import set_design_rules, high_level_action
from ...command_api import dispatch_command
from ...expansion import expand_high_level_action
from ...debug import traced

ACTION_NAME = "set_design_rules"


@traced(scope="actions.set_design_rules.main")
def main(*, board, command):
    return dispatch_command(operation=set_design_rules, board=board, command=command,
                            required_keys={"reason"})


@traced(scope="actions.set_design_rules.get_high_level_action")
def get_high_level_action(**kwargs):
    return high_level_action(ACTION_NAME, **kwargs)


@traced(scope="actions.set_design_rules.expand_action")
def expand_action(*, action, **kwargs):
    return expand_high_level_action(action=action, **kwargs)
