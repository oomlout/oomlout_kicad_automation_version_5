"""Declare copper pour geometry for deterministic Konnect execution."""
from ...authoring import copper_pour, high_level_action
from ...command_api import dispatch_command
from ...expansion import expand_high_level_action
from ...debug import traced
ACTION_NAME='copper_pour'

@traced(scope='actions.copper_pour.main')
def main(*, board, command):
    return dispatch_command(operation=copper_pour,board=board,command=command,
        required_keys={'name','net','layer','points_mm','clearance_mm','min_width_mm','pad_connection','priority','reason'})

@traced(scope='actions.copper_pour.get_high_level_action')
def get_high_level_action(**kwargs):
    return high_level_action(ACTION_NAME,**kwargs)

@traced(scope='actions.copper_pour.expand_action')
def expand_action(*, action, **kwargs):
    return expand_high_level_action(action=action,**kwargs)
