"""Explicit design-time action; intentionally excluded from board execution."""
from ...svg_layout import plan_layout
from ...debug import traced

ACTION_NAME='plan_svg_layout'


@traced(scope='actions.plan_svg_layout.main')
def main(*, board, command):
    return plan_layout(board=board,command=command)


@traced(scope='actions.plan_svg_layout.get_high_level_action')
def get_high_level_action(**kwargs):
    return {'action':ACTION_NAME,'status':'declared','phase':'design_time','inputs':kwargs}


@traced(scope='actions.plan_svg_layout.expand_action')
def expand_action(*, action, **kwargs):
    raise ValueError('Review/promote explicit placements and route_path dictionaries; never optimize during compile')
