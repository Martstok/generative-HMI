"""Build synthetic ScreenSpec objects for drill-down views."""

from __future__ import annotations

from tools.gui.hmi_models import ScreenSpec, TagRef, Widget, WidgetType

# Each drivetrain anchor maps to (anchor, tag path suffix after PLC:, label, unit, format)
_DRIVETRAIN_BINDINGS: list[tuple[str, str, str, str, str]] = [
    ("wind_speed",        '"HMI_DB".Conditions.rWindSpeed_mps',    "WIND",    "m/s",  ".1f"),
    ("rotor_rpm",         '"HMI_DB".Rotor.rRotorRPM',              "ROTOR",   "rpm",  ".1f"),
    ("pitch",             '"HMI_DB".Rotor.rPitchAngle_deg',        "PITCH",   "\u00b0",    ".1f"),
    ("gearbox_oil_temp",  '"HMI_DB".Gearbox.rOilTemp_C',           "GB OIL",  "\u00b0C",   ".0f"),
    ("gearbox_vibration", '"HMI_DB".Gearbox.rVibration_mm_s',      "VIB",     "mm/s", ".2f"),
    ("gen_rpm",           '"HMI_DB".Power.rGeneratorRPM',          "GEN",     "rpm",  ".0f"),
    ("gen_power",         '"HMI_DB".Power.rActivePower_kW',        "POWER",   "kW",   ".0f"),
    ("nacelle_temp",      '"HMI_DB".Nacelle.rNacelleTemp_C',       "NACELLE", "\u00b0C",   ".0f"),
    ("yaw_position",      '"HMI_DB".Yaw.rYawPosition_deg',         "YAW",     "\u00b0",    ".0f"),
    ("grid_power",        '"HMI_DB".Power.rActivePower_kW',        "GRID",    "kW",   ".0f"),
]


def build_drivetrain_spec(plc_name: str) -> ScreenSpec:
    """Construct an in-memory ScreenSpec for a turbine_drivetrain widget
    pre-bound to all anchors for the given WTG PLC."""
    tags = [
        TagRef(
            tag=f"{plc_name}:{suffix}",
            label=label,
            unit=unit,
            format=fmt,
            anchor=anchor,
        )
        for anchor, suffix, label, unit, fmt in _DRIVETRAIN_BINDINGS
    ]
    widget = Widget(
        id="w1",
        type=WidgetType.SCHEMATIC,
        title=f"{plc_name} Drivetrain",
        diagram="turbine_drivetrain",
        columns=4,
        tags=tags,
    )
    return ScreenSpec(
        title=f"{plc_name} Drivetrain",
        description=f"Live mechanical/electrical chain for {plc_name}",
        columns=4,
        widgets=[widget],
        poll_interval_ms=2000,
    )
