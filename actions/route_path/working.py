"""Declare a reviewed, explicit copper polyline in board-local millimetres."""
from ...authoring import route_path, high_level_action
from ...command_api import dispatch_command
from ...expansion import expand_high_level_action
from ...debug import traced
ACTION_NAME='route_path'

@traced(scope='actions.route_path.main')
def main(*, board, command):
    return dispatch_command(operation=route_path, board=board, command=command,
                            required_keys={'name','net','layer','width_mm','start','end','points_mm','reason'})

@traced(scope='actions.route_path.get_high_level_action')
def get_high_level_action(**kwargs):
    return high_level_action(ACTION_NAME, **kwargs)

@traced(scope='actions.route_path.expand_action')
def expand_action(*, action, **kwargs):
    return expand_high_level_action(action=action, **kwargs)
