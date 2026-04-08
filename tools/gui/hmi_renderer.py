"""Renderer that converts a ScreenSpec into HTML via Jinja2 widget templates."""

from __future__ import annotations

from tools.gui.hmi_models import ScreenSpec, WidgetType

# Map widget types to their template paths
WIDGET_TEMPLATES = {
    WidgetType.VALUE_CARD: "partials/hmi/value_card.html",
    WidgetType.GAUGE: "partials/hmi/gauge.html",
    WidgetType.STATUS: "partials/hmi/status.html",
    WidgetType.BAR_CHART: "partials/hmi/bar_chart.html",
    WidgetType.TABLE: "partials/hmi/table.html",
    WidgetType.TREND_CHART: "partials/hmi/trend_chart.html",
    WidgetType.ALARM_LIST: "partials/hmi/alarm_list.html",
    WidgetType.COMMAND: "partials/hmi/command.html",
    WidgetType.SCHEMATIC: "partials/hmi/schematic.html",
}


def render_widgets(spec: ScreenSpec) -> list[str]:
    """Render each widget in the spec to an HTML string."""
    from tools.gui import templates

    result = []
    for widget in spec.widgets:
        tpl_path = WIDGET_TEMPLATES[widget.type]
        tpl = templates.env.get_template(tpl_path)
        html = tpl.render(widget=widget)
        result.append(html)
    return result
