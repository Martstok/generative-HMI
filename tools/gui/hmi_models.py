"""Pydantic models for the Generative HMI screen spec and tag catalog."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class WidgetType(str, Enum):
    VALUE_CARD = "value_card"
    GAUGE = "gauge"
    BAR_CHART = "bar_chart"
    STATUS = "status"
    TABLE = "table"
    TREND_CHART = "trend_chart"
    ALARM_LIST = "alarm_list"
    COMMAND = "command"
    SCHEMATIC = "schematic"


class TagRef(BaseModel):
    tag: str = Field(description="Fully qualified tag path, e.g. '\"Winch_DB\".rSetpoint'")
    label: str = Field(description="Human-readable label for display")
    unit: str = Field(default="", description="Engineering unit, e.g. '%', 'RPM', 'bar'")
    format: str = Field(default=".1f", description="Python format spec for numeric display")
    anchor: str = Field(default="", description="Anchor name inside the diagram SVG; binds this tag to a fixed position")


class Widget(BaseModel):
    id: str = Field(description="Unique widget ID (CSS-safe, e.g. 'w1')")
    type: WidgetType
    title: str = Field(description="Widget heading")
    tags: list[TagRef] = Field(description="Tag references bound to this widget")
    min_value: float | None = Field(default=None, description="For gauge/bar/trend: minimum scale value")
    max_value: float | None = Field(default=None, description="For gauge/bar/trend: maximum scale value")
    columns: int = Field(default=1, description="Grid column span (1-4)")
    severity: str = Field(default="warning", description="For alarm_list: default severity (info/warning/critical)")
    write_value: str = Field(default="true", description="For command: value to write when button pressed")
    diagram: str = Field(default="", description="Diagram key for schematic widget: farm_sld, substation_sld, turbine_drivetrain")


class ScreenSpec(BaseModel):
    title: str = Field(description="Screen title displayed at top")
    description: str = Field(default="", description="Brief explanation of what the screen shows")
    columns: int = Field(default=3, description="Grid column count (1-4)")
    widgets: list[Widget]
    poll_interval_ms: int = Field(default=2000, description="Tag polling interval in milliseconds")
    instance_name: str = Field(default="", description="PLCSIM instance name for tag reads")
    opcua_endpoint: str = Field(default="", description="OPC UA endpoint URL, e.g. 'opc.tcp://192.168.0.1:4840'")
    prompt: str = Field(default="", description="Original user prompt that generated this screen")

    def all_tags(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for w in self.widgets:
            for t in w.tags:
                if t.tag not in seen:
                    seen.add(t.tag)
                    result.append(t.tag)
        return result


class TagEntry(BaseModel):
    name: str = Field(description="Fully qualified tag path")
    data_type: str = Field(description="PLC data type: Bool, Real, Int, etc.")
    block: str = Field(description="Parent block name")
    description: str = Field(default="", description="From inline SCL comment")
    category: str = Field(default="general", description="Heuristic category")


class TagCatalog(BaseModel):
    project: str
    plc_name: str
    built_at: str
    tags: list[TagEntry]
