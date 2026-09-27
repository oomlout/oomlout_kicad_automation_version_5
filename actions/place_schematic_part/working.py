"""Set final authored schematic placement for an instance."""

from ...authoring import high_level_action, place_schematic_part
from ...command_api import dispatch_command
from ...expansion import expand_high_level_action
from ...debug import traced

ACTION_NAME = "place_schematic_part"


@traced(scope="actions.place_schematic_part.main")
def main(*, board, command):
    return dispatch_command(
        operation=place_schematic_part,
        board=board,
        command=command,
        required_keys={"part", "schematic_x", "schematic_y"},
    )


@traced(scope="actions.place_schematic_part.get_high_level_action")
def get_high_level_action(**kwargs):
    return high_level_action(ACTION_NAME, **kwargs)


@traced(scope="actions.place_schematic_part.expand_action")
def expand_action(*, action, **kwargs):
    return expand_high_level_action(action=action, **kwargs)
