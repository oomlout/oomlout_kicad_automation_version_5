"""Reusable Python-first KiCad automation helpers."""

from .authoring import (
    MOUNTING_HOLE_OOMP_ID,
    compare_declaration_fingerprint,
    evaluate_board_definition,
    finalize_declarations,
    new_board,
)
from .connectivity import (
    allocate_references,
    compile_connectivity,
    parse_symbol_file,
    parse_symbol_text,
    symbols_from_inspections,
)
from .footprints import bounds_from_inspections, footprint_bounds_file, footprint_bounds_text
from .geometry import board_geometry, resolve_block_transforms, resolve_geometry
from .plans import compile_frozen_bundle, verify_frozen_bundle
from .working import REQUIRED_ACTION_CALLABLES, compile_board, discover_actions

__all__ = [
    "MOUNTING_HOLE_OOMP_ID",
    "REQUIRED_ACTION_CALLABLES",
    "compare_declaration_fingerprint",
    "compile_board",
    "compile_connectivity",
    "compile_frozen_bundle",
    "discover_actions",
    "evaluate_board_definition",
    "finalize_declarations",
    "new_board",
    "allocate_references",
    "parse_symbol_file",
    "parse_symbol_text",
    "symbols_from_inspections",
    "board_geometry",
    "bounds_from_inspections",
    "footprint_bounds_file",
    "footprint_bounds_text",
    "resolve_block_transforms",
    "resolve_geometry",
    "verify_frozen_bundle",
]
