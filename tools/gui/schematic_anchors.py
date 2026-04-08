"""Per-diagram schematic anchor registry.

Boolean anchors preserve their shape on OOB swap (server emits the matching
SVG fragment). Numeric anchors emit text only.
"""
from __future__ import annotations

BOOLEAN_ANCHORS: dict[str, set[str]] = {
    "farm_sld": {
        "wtg1_status", "wtg2_status", "wtg3_status", "wtg4_status",
        "export_breaker",
    },
    "substation_sld": {
        "incomer_a", "incomer_b", "busbar_a", "busbar_b", "tie_breaker",
        "export_breaker",
    },
    "turbine_drivetrain": set(),
}

_STATUS_DOT = '<circle class="sch-equip" cx="0" cy="0" r="5"></circle>'
_BREAKER_H = (
    '<line class="sch-connector" x1="0" y1="20" x2="54" y2="20"></line>'
    '<circle class="sch-equip" cx="0" cy="20" r="3"></circle>'
    '<circle class="sch-equip" cx="54" cy="20" r="3"></circle>'
    '<rect class="sch-equip" x="22" y="14" width="12" height="12"></rect>'
)
_BREAKER_V = (
    '<line class="sch-connector" x1="0" y1="0" x2="0" y2="40"></line>'
    '<circle class="sch-equip" cx="0" cy="0" r="3"></circle>'
    '<circle class="sch-equip" cx="0" cy="40" r="3"></circle>'
    '<rect class="sch-equip" x="-6" y="14" width="12" height="12"></rect>'
)
_BUS_SEG = '<line class="sch-bus" x1="0" y1="0" x2="40" y2="0"></line>'

BOOLEAN_ANCHOR_SHAPES: dict[tuple[str, str], str] = {
    # farm_sld
    ("farm_sld", "wtg1_status"): _STATUS_DOT,
    ("farm_sld", "wtg2_status"): _STATUS_DOT,
    ("farm_sld", "wtg3_status"): _STATUS_DOT,
    ("farm_sld", "wtg4_status"): _STATUS_DOT,
    ("farm_sld", "export_breaker"): _BREAKER_H,
    # substation_sld
    ("substation_sld", "incomer_a"): _BREAKER_V,
    ("substation_sld", "incomer_b"): _BREAKER_V,
    ("substation_sld", "busbar_a"): _BUS_SEG,
    ("substation_sld", "busbar_b"): _BUS_SEG,
    ("substation_sld", "tie_breaker"): _BREAKER_V,
    ("substation_sld", "export_breaker"): _BREAKER_V,
}


def is_boolean_anchor(diagram: str, anchor: str) -> bool:
    return anchor in BOOLEAN_ANCHORS.get(diagram, set())
