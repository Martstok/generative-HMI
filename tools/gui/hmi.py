"""HMI screen routes for the TIA Dashboard."""

from __future__ import annotations

import json
import math
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from tools.gui import client, templates
from tools.gui.hmi_history import history
from tools.gui.hmi_models import ScreenSpec, WidgetType
from tools.gui.hmi_renderer import render_widgets
from tools.gui.project_endpoints import (
    group_tags_by_plc,
    load_endpoints,
    split_prefixed_tag,
)
from tools.gui.tag_catalog import build_catalog, load_catalog, save_catalog

router = APIRouter()

# Root of the repository
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Active screen generation tasks
_active_generations: dict[str, object] = {}

# Alarm state tracking: tag -> {active: bool, since: epoch_ms}
_alarm_states: dict[str, dict] = {}


def _render(request: Request, template: str, **context):
    """Render a Jinja2 template with the given context."""
    return templates.TemplateResponse(request, template, context)


# ---------------------------------------------------------------------------
# Helper: find the active project directory
# ---------------------------------------------------------------------------


def _find_project_dir() -> Path | None:
    """Detect the active project directory.

    Strategy:
    1. Query the daemon for status and match the project_path back to a
       ``projects/<name>/`` directory.
    2. Fall back to the first ``projects/*/project.json`` found.
    """
    projects_root = _REPO_ROOT / "projects"

    # Strategy 1: ask the daemon
    try:
        resp = client.send("status")
        if resp.ok and resp.data:
            daemon_project_path = resp.data.get("project") or resp.data.get("project_path", "")
            if daemon_project_path:
                daemon_path = Path(daemon_project_path).resolve()
                # Scan project dirs and match by project_path
                for pj_file in sorted(projects_root.glob("*/project.json")):
                    try:
                        pj = json.loads(pj_file.read_text(encoding="utf-8-sig"))
                        stored = Path(pj.get("project_path", "")).resolve()
                        if stored == daemon_path:
                            return pj_file.parent
                    except (json.JSONDecodeError, OSError):
                        continue
    except Exception:
        pass

    # Strategy 2: fall back to first project with a project.json
    if projects_root.is_dir():
        for pj_file in sorted(projects_root.glob("*/project.json")):
            return pj_file.parent

    return None


def _screens_dir(project_dir: Path) -> Path:
    """Return the hmi_screens directory for a project."""
    return project_dir / "hmi_screens"


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def hmi_browser(request: Request):
    """Home page -- generate new screens and browse saved ones."""
    project_dir = _find_project_dir()
    screens: list[dict] = []

    if project_dir:
        sdir = _screens_dir(project_dir)
        if sdir.is_dir():
            for json_file in sorted(sdir.glob("*.json")):
                try:
                    spec = ScreenSpec.model_validate_json(
                        json_file.read_text(encoding="utf-8-sig")
                    )
                    screens.append(
                        {
                            "name": json_file.stem,
                            "title": spec.title,
                            "description": spec.description,
                            "widget_count": len(spec.widgets),
                            "tag_count": len(spec.all_tags()),
                        }
                    )
                except Exception:
                    # Skip malformed files
                    continue

    return _render(request, "hmi.html", screens=screens)


@router.get("/hmi/screen/{name}", response_class=HTMLResponse)
async def hmi_screen(request: Request, name: str):
    """Render a saved HMI screen with live tag polling."""
    project_dir = _find_project_dir()
    if not project_dir:
        return HTMLResponse("<p>No active project found.</p>", status_code=404)

    spec_path = _screens_dir(project_dir) / f"{name}.json"
    if not spec_path.exists():
        return HTMLResponse(f"<p>Screen '{name}' not found.</p>", status_code=404)

    spec = ScreenSpec.model_validate_json(spec_path.read_text(encoding="utf-8-sig"))
    widget_htmls = render_widgets(spec)
    all_tags_csv = ",".join(spec.all_tags())

    return _render(
        request,
        "hmi_screen.html",
        spec=spec,
        widget_htmls=widget_htmls,
        all_tags_csv=all_tags_csv,
        spec_name=name,
    )


@router.get("/hmi/drilldown/{plc_name}", response_class=HTMLResponse)
async def hmi_drilldown(request: Request, plc_name: str):
    """Render an in-memory drill-down screen for a specific PLC."""
    project_dir = _find_project_dir()
    valid_plcs: set[str] = set()
    if project_dir:
        valid_plcs = {n for n, _ in load_endpoints(project_dir)}
    if plc_name not in valid_plcs:
        return HTMLResponse(f"<p>Unknown PLC: {plc_name}</p>", status_code=404)
    if not plc_name.startswith("WTG"):
        return HTMLResponse(
            "<p>Drill-down only available for turbines (WTGxx).</p>",
            status_code=400,
        )

    from tools.gui.drilldown import build_drivetrain_spec

    spec = build_drivetrain_spec(plc_name)
    widget_htmls = render_widgets(spec)
    all_tags_csv = ",".join(spec.all_tags())
    return _render(
        request,
        "hmi_screen.html",
        spec=spec,
        widget_htmls=widget_htmls,
        all_tags_csv=all_tags_csv,
        spec_name="",
    )


# ---------------------------------------------------------------------------
# API routes -- htmx fragments
# ---------------------------------------------------------------------------


def _format_value(raw_value, fmt: str) -> str:
    """Format a raw PLC value using a Python format spec, with graceful fallback."""
    if raw_value is None:
        return "\u2014"
    try:
        return format(float(raw_value), fmt)
    except (ValueError, TypeError):
        # Boolean or string values -- just stringify
        return str(raw_value)


def _clamp_pct(value, min_val: float | None, max_val: float | None) -> float:
    """Compute a percentage from value within [min_val, max_val], clamped 0-100."""
    mn = min_val if min_val is not None else 0.0
    mx = max_val if max_val is not None else 100.0
    if mx <= mn:
        return 0.0
    try:
        pct = (float(value) - mn) / (mx - mn) * 100.0
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, min(100.0, pct))


# Trend chart colour palette
_TREND_COLORS = ["#3b82f6", "#06b6d4", "#f59e0b", "#ef4444", "#8b5cf6", "#10b981"]


def _render_trend_svg(widget, tag_values: dict) -> str:
    """Build a complete <svg> element for a trend chart widget."""
    from tools.gui.hmi_history import history

    W, H = 600, 200
    PAD_L, PAD_R, PAD_T, PAD_B = 45, 10, 10, 20
    chart_w = W - PAD_L - PAD_R
    chart_h = H - PAD_T - PAD_B
    wid = widget.id

    # Gather history for all tags
    all_series: list[list[dict]] = []
    for tag_ref in widget.tags:
        all_series.append(history.get(tag_ref.tag))

    # Determine Y-axis range
    if widget.min_value is not None and widget.max_value is not None:
        y_min, y_max = widget.min_value, widget.max_value
    else:
        all_vals = [s["v"] for series in all_series for s in series]
        if all_vals:
            y_min, y_max = min(all_vals), max(all_vals)
            margin = (y_max - y_min) * 0.1 or 1.0
            y_min -= margin
            y_max += margin
        else:
            y_min, y_max = 0.0, 100.0

    y_range = y_max - y_min if y_max != y_min else 1.0

    # Determine X-axis range (last 60s or whatever history covers)
    all_times = [s["t"] for series in all_series for s in series]
    if all_times:
        t_max = max(all_times)
        t_min = t_max - 60000  # 60 seconds window
    else:
        t_max = int(time.time() * 1000)
        t_min = t_max - 60000
    t_range = t_max - t_min if t_max != t_min else 1

    parts: list[str] = []
    parts.append(
        f'<svg id="trend-{wid}" hx-swap-oob="true"'
        f' viewBox="0 0 {W} {H}" preserveAspectRatio="none"'
        f' class="trend-svg">'
    )

    # Background
    parts.append(
        f'<rect x="{PAD_L}" y="{PAD_T}" width="{chart_w}" height="{chart_h}"'
        f' fill="#0d1117" rx="1"/>'
    )

    # Gridlines (4 horizontal)
    for i in range(1, 4):
        gy = PAD_T + chart_h * i / 4
        parts.append(
            f'<line x1="{PAD_L}" y1="{gy:.0f}" x2="{W - PAD_R}" y2="{gy:.0f}"'
            f' stroke="#1e293b" stroke-width="0.5"/>'
        )
        # Y-axis label
        label_val = y_max - (y_max - y_min) * i / 4
        parts.append(
            f'<text x="{PAD_L - 4}" y="{gy + 3:.0f}" text-anchor="end"'
            f' font-size="9" fill="#64748b" font-family="\'JetBrains Mono\', monospace">'
            f'{label_val:.0f}</text>'
        )

    # Top/bottom Y labels
    parts.append(
        f'<text x="{PAD_L - 4}" y="{PAD_T + 8}" text-anchor="end"'
        f' font-size="9" fill="#64748b" font-family="\'JetBrains Mono\', monospace">'
        f'{y_max:.0f}</text>'
    )
    parts.append(
        f'<text x="{PAD_L - 4}" y="{H - PAD_B}" text-anchor="end"'
        f' font-size="9" fill="#64748b" font-family="\'JetBrains Mono\', monospace">'
        f'{y_min:.0f}</text>'
    )

    # Polylines for each tag
    for series_idx, series in enumerate(all_series):
        if len(series) < 2:
            continue
        color = _TREND_COLORS[series_idx % len(_TREND_COLORS)]
        points: list[str] = []
        for s in series:
            x = PAD_L + (s["t"] - t_min) / t_range * chart_w
            y = PAD_T + chart_h - (s["v"] - y_min) / y_range * chart_h
            x = max(PAD_L, min(W - PAD_R, x))
            y = max(PAD_T, min(PAD_T + chart_h, y))
            points.append(f"{x:.1f},{y:.1f}")
        parts.append(
            f'<polyline points="{" ".join(points)}" fill="none"'
            f' stroke="{color}" stroke-width="1.5" stroke-linejoin="round"/>'
        )

    # "No data" placeholder
    if not any(len(s) >= 2 for s in all_series):
        parts.append(
            f'<text x="{W / 2}" y="{H / 2}" text-anchor="middle"'
            f' font-size="12" fill="#64748b">Waiting for data\u2026</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


def _gauge_dasharray(pct: float) -> str:
    """Compute SVG stroke-dasharray for the gauge arc.

    The arc path length is approximately 220 units (half-circle of radius 70).
    """
    arc_length = 220.0
    filled = arc_length * pct / 100.0
    return f"{filled:.1f} {arc_length:.1f}"


@router.get("/api/hmi/tags", response_class=HTMLResponse)
async def hmi_tag_poll(
    request: Request,
    instance: str = Query(""),
    tags: str = Query(""),
    spec_name: str = Query(""),
):
    """Live tag polling endpoint -- returns OOB swap fragments for htmx."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if not tag_list:
        return HTMLResponse("")

    # Load the screen spec early to check data source
    spec: ScreenSpec | None = None
    project_dir = _find_project_dir()
    if project_dir and spec_name:
        spec_path = _screens_dir(project_dir) / f"{spec_name}.json"
        if spec_path.exists():
            try:
                spec = ScreenSpec.model_validate_json(
                    spec_path.read_text(encoding="utf-8-sig")
                )
            except Exception:
                pass

    # Read tag values -- prefer OPC UA, fall back to PLCSIM via daemon
    tag_values: dict[str, object] = {}
    read_ok = False

    endpoints_list = load_endpoints(project_dir) if project_dir else []
    endpoint_map = {name: ep for name, ep in endpoints_list}
    legacy_endpoint = spec.opcua_endpoint if spec else ""
    default_plc: str | None = None
    if not endpoint_map and legacy_endpoint:
        endpoint_map = {"PLC": legacy_endpoint}
        default_plc = "PLC"
    elif len(endpoint_map) == 1:
        default_plc = next(iter(endpoint_map))

    if endpoint_map:
        try:
            from tools.gui.opcua_client import get_opcua_client
            import asyncio as _asyncio

            groups = group_tags_by_plc(tag_list, default_plc=default_plc)

            async def _read_group(plc_name, prefixed_tags):
                endpoint = endpoint_map.get(plc_name)
                if not endpoint:
                    return {t: None for t in prefixed_tags}
                # Strip prefix (if any) for the actual read
                clean_map: dict[str, str] = {}
                for ptag in prefixed_tags:
                    _, remainder = split_prefixed_tag(ptag)
                    clean_map[ptag] = remainder
                try:
                    opcua = get_opcua_client(endpoint)
                    raw = await opcua.read_tags(list(clean_map.values()))
                except Exception:
                    return {t: None for t in prefixed_tags}
                return {ptag: raw.get(clean) for ptag, clean in clean_map.items()}

            tasks = [_read_group(plc, tags) for plc, tags in groups.items() if plc]
            unroutable = groups.get(None, [])
            for u in unroutable:
                tag_values[u] = None

            if tasks:
                group_results = await _asyncio.gather(*tasks)
                for g in group_results:
                    tag_values.update(g)
            read_ok = any(v is not None for v in tag_values.values())
        except Exception:
            pass
    elif instance:
        try:
            resp = client.send(
                "sim_io",
                {"instance_name": instance, "action": "read_tags", "tag_names": tag_list},
            )
            if resp.ok and resp.data and "tags" in resp.data:
                tag_values = resp.data["tags"]
                read_ok = True
        except Exception:
            pass

    # Feed tag history ring buffer
    if tag_values:
        history.record(tag_values)

    # Build OOB swap fragments
    fragments: list[str] = []

    if spec:
        for widget in spec.widgets:
            wid = widget.id

            # ── Trend chart: replace the full SVG with rendered history ──
            if widget.type == WidgetType.TREND_CHART:
                fragments.append(_render_trend_svg(widget, tag_values))
                # Also emit value spans for legend current values
                for tag_idx, tag_ref in enumerate(widget.tags):
                    raw = tag_values.get(tag_ref.tag)
                    formatted = _format_value(raw, tag_ref.format) if raw is not None else "\u2014"
                    unit_suffix = f" {tag_ref.unit}" if tag_ref.unit else ""
                    fragments.append(
                        f'<span id="val-{wid}-{tag_idx}" class="trend-val" hx-swap-oob="true">'
                        f"{formatted}{unit_suffix}</span>"
                    )
                continue

            # ── Alarm list: replace each alarm row ──
            if widget.type == WidgetType.ALARM_LIST:
                now_ms = int(time.time() * 1000)
                for tag_idx, tag_ref in enumerate(widget.tags):
                    raw = tag_values.get(tag_ref.tag)
                    is_active = False
                    if raw is not None:
                        is_active = str(raw).lower() in ("true", "1", "1.0")

                    # Track state transitions for timestamps + ack
                    state_key = f"{wid}-{tag_ref.tag}"
                    prev = _alarm_states.get(state_key)
                    if prev is None or prev.get("active") != is_active:
                        # Transition: reset ack on either edge
                        _alarm_states[state_key] = {
                            "active": is_active,
                            "since": now_ms,
                            "acked": False,
                            "ack_time": None,
                        }
                    entry = _alarm_states[state_key]
                    since = entry["since"]
                    acked = entry.get("acked", False)
                    ack_time = entry.get("ack_time")

                    # Format timestamp as relative
                    elapsed_s = (now_ms - since) / 1000
                    if elapsed_s < 60:
                        time_str = f"{int(elapsed_s)}s ago"
                    elif elapsed_s < 3600:
                        time_str = f"{int(elapsed_s / 60)}m ago"
                    else:
                        time_str = f"{int(elapsed_s / 3600)}h ago"

                    sev = widget.severity
                    label = tag_ref.label or tag_ref.tag

                    if is_active and not acked:
                        dot_cls = f"alarm-dot alarm-dot-{sev}"
                        # state_key may contain double quotes from tag path —
                        # HTML-escape for the attribute value.
                        sk_attr = state_key.replace("&", "&amp;").replace("\"", "&quot;").replace("'", "&#39;")
                        fragments.append(
                            f'<div id="alarm-{wid}-{tag_idx}" class="alarm-row alarm-active" hx-swap-oob="true">'
                            f'<div class="{dot_cls}"></div>'
                            f'<span class="alarm-label">{label}</span>'
                            f'<span id="val-{wid}-{tag_idx}" class="alarm-status">ACTIVE</span>'
                            f'<span class="alarm-time">{time_str}</span>'
                            f'<button class="alarm-ack-btn" data-key="{sk_attr}" onclick="ackAlarmBtn(this)">ACK</button>'
                            f'</div>'
                        )
                    elif is_active and acked:
                        dot_cls = f"alarm-dot alarm-dot-{sev}"
                        ack_elapsed = 0
                        if ack_time is not None:
                            ack_elapsed = int((now_ms - ack_time) / 1000)
                        fragments.append(
                            f'<div id="alarm-{wid}-{tag_idx}" class="alarm-row alarm-active alarm-acked" hx-swap-oob="true">'
                            f'<div class="{dot_cls}"></div>'
                            f'<span class="alarm-label">{label}</span>'
                            f'<span id="val-{wid}-{tag_idx}" class="alarm-status">ACTIVE</span>'
                            f'<span class="alarm-time">{time_str}</span>'
                            f'<span class="alarm-ack-time">ACK\'d {ack_elapsed}s ago</span>'
                            f'</div>'
                        )
                    else:
                        dot_cls = "alarm-dot alarm-dot-ok"
                        fragments.append(
                            f'<div id="alarm-{wid}-{tag_idx}" class="alarm-row" hx-swap-oob="true">'
                            f'<div class="{dot_cls}"></div>'
                            f'<span class="alarm-label">{label}</span>'
                            f'<span id="val-{wid}-{tag_idx}" class="alarm-status">OK</span>'
                            f'<span class="alarm-time">{time_str}</span>'
                            f'</div>'
                        )
                continue

            # ── Command: update status spans ──
            if widget.type == WidgetType.COMMAND:
                for tag_idx, tag_ref in enumerate(widget.tags):
                    raw = tag_values.get(tag_ref.tag)
                    if raw is not None:
                        is_on = str(raw).lower() in ("true", "1", "1.0")
                        status = "ON" if is_on else "OFF"
                        cls = "cmd-status cmd-on" if is_on else "cmd-status cmd-off"
                    else:
                        status = "\u2014"
                        cls = "cmd-status"
                    fragments.append(
                        f'<span id="val-{wid}-{tag_idx}" class="{cls}" hx-swap-oob="true">'
                        f"{status}</span>"
                    )
                continue

            # ── Schematic: replace each anchor group ──
            if widget.type == WidgetType.SCHEMATIC:
                from tools.gui.schematic_anchors import (
                    is_boolean_anchor, BOOLEAN_ANCHOR_SHAPES,
                )
                for tag_ref in widget.tags:
                    if not tag_ref.anchor:
                        continue
                    raw = tag_values.get(tag_ref.tag)
                    anchor = tag_ref.anchor
                    is_bool_anchor = is_boolean_anchor(widget.diagram, anchor)

                    if is_bool_anchor:
                        shape = BOOLEAN_ANCHOR_SHAPES.get(
                            (widget.diagram, anchor), ""
                        )
                        if raw is None:
                            state_cls = "sch-state-off"
                        elif isinstance(raw, bool):
                            state_cls = "sch-state-on" if raw else "sch-state-off"
                        else:
                            state_cls = "sch-state-on" if raw else "sch-state-off"
                        fragments.append(
                            f'<g id="sch-{wid}-{anchor}" '
                            f'class="sch-anchor {state_cls}" hx-swap-oob="true">'
                            f'{shape}<text opacity="0">_</text></g>'
                        )
                    else:
                        # Numeric anchor — text-only swap
                        if raw is None:
                            display = "\u2014"
                        elif isinstance(raw, bool):
                            display = "CLOSED" if raw else "OPEN"
                        else:
                            display = _format_value(raw, tag_ref.format)
                        unit = tag_ref.unit or ""
                        label = tag_ref.label or anchor.replace("_", " ").upper()
                        fragments.append(
                            f'<g id="sch-{wid}-{anchor}" class="sch-anchor" hx-swap-oob="true">'
                            f'<text class="sch-label" x="0" y="0">{label}</text>'
                            f'<text class="sch-value" x="0" y="14">{display}</text>'
                            f'<text class="sch-unit" x="0" y="23">{unit}</text>'
                            f'</g>'
                        )
                continue

            # ── Original widget types ──
            for tag_idx, tag_ref in enumerate(widget.tags):
                raw = tag_values.get(tag_ref.tag)
                fallback = "\u2014"

                if raw is not None:
                    formatted = _format_value(raw, tag_ref.format)
                else:
                    formatted = fallback

                # Value span -- used by all widget types
                if widget.type == WidgetType.BAR_CHART:
                    unit_suffix = f" {tag_ref.unit}" if tag_ref.unit else ""
                    fragments.append(
                        f'<span id="val-{wid}-{tag_idx}" hx-swap-oob="true">'
                        f"{formatted}{unit_suffix}</span>"
                    )
                elif widget.type == WidgetType.GAUGE:
                    pct = _clamp_pct(raw, widget.min_value, widget.max_value)
                    da = _gauge_dasharray(pct)
                    fragments.append(
                        f'<svg id="gauge-{wid}-{tag_idx}" hx-swap-oob="true"'
                        f' viewBox="0 0 200 120" width="200" height="120">'
                        f'<path d="M 30 100 A 70 70 0 0 1 170 100"'
                        f' fill="none" stroke="#1e293b" stroke-width="12" stroke-linecap="round"/>'
                        f'<path d="M 30 100 A 70 70 0 0 1 170 100"'
                        f' fill="none" stroke="#3b82f6" stroke-width="12"'
                        f' stroke-linecap="round" stroke-dasharray="{da}"/>'
                        f'<text x="100" y="95" text-anchor="middle" font-size="20"'
                        f' font-weight="700" font-family="\'JetBrains Mono\', monospace"'
                        f' fill="#e2e8f0">{formatted}</text>'
                        f'</svg>'
                    )
                else:
                    cls = "value-number" if widget.type == WidgetType.VALUE_CARD else ""
                    fragments.append(
                        f'<span id="val-{wid}-{tag_idx}" class="{cls}" hx-swap-oob="true">'
                        f"{formatted}</span>"
                    )

                if widget.type == WidgetType.STATUS:
                    is_on = False
                    if raw is not None:
                        is_on = str(raw).lower() in ("true", "1", "1.0")
                    dot_class = "on" if is_on else "off"
                    fragments.append(
                        f'<div id="dot-{wid}-{tag_idx}" hx-swap-oob="true"'
                        f' class="status-dot {dot_class}"></div>'
                    )

                if widget.type == WidgetType.BAR_CHART:
                    pct = _clamp_pct(raw, widget.min_value, widget.max_value)
                    fragments.append(
                        f'<div id="bar-{wid}-{tag_idx}" hx-swap-oob="true"'
                        f' class="bar-fill" style="width: {pct:.1f}%"></div>'
                    )

    return HTMLResponse("\n".join(fragments))


@router.post("/api/hmi/generate", response_class=HTMLResponse)
async def hmi_generate(
    request: Request, prompt: str = Form(...), model: str = Form(""),
):
    """Start HMI screen generation from a natural language prompt."""
    project_dir = _find_project_dir()
    if not project_dir:
        return HTMLResponse(
            '<article><p>No active project found.</p></article>',
            status_code=500,
        )

    if len(_active_generations) >= 2:
        return HTMLResponse(
            '<article><p>Too many concurrent generations. Please wait.</p></article>',
            status_code=429,
        )

    from tools.gui.hmi_generator import ALLOWED_MODELS, generate_screen
    selected_model = model if model in ALLOWED_MODELS else None

    gen_id = uuid.uuid4().hex[:8]
    _active_generations[gen_id] = generate_screen(
        prompt, project_dir, model=selected_model,
    )

    return HTMLResponse(
        f'<div id="gen-progress" hx-ext="sse"'
        f' sse-connect="/api/hmi/generate/{gen_id}/stream"'
        f' sse-swap="progress">'
        f'<p aria-busy="true">Starting generation...</p>'
        f'</div>'
    )


@router.get("/api/hmi/generate/{gen_id}/stream")
async def hmi_generate_stream(gen_id: str):
    """SSE stream for screen generation progress."""
    generator = _active_generations.get(gen_id)
    if not generator:
        return HTMLResponse("Generation not found", status_code=404)

    async def _event_stream():
        try:
            async for event in generator:
                etype = event.get("type", "")
                if etype == "progress":
                    msg = event.get("message", "")
                    html = f'<p aria-busy="true">{msg}</p>'
                    yield f"event: progress\ndata: {html}\n\n"
                elif etype == "complete":
                    name = event.get("screen_name", "")
                    html = (
                        f'<p>Screen <strong>{name}</strong> created!</p>'
                        f'<script>setTimeout(function(){{window.location="/hmi/screen/{name}"}},1500)</script>'
                    )
                    yield f"event: progress\ndata: {html}\n\n"
                elif etype == "error":
                    msg = event.get("message", "Unknown error")
                    html = f'<article><p>Error: {msg}</p></article>'
                    yield f"event: progress\ndata: {html}\n\n"
        finally:
            _active_generations.pop(gen_id, None)

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


@router.post("/api/hmi/refine/{name}", response_class=HTMLResponse)
async def hmi_refine(
    request: Request, name: str, prompt: str = Form(...), model: str = Form(""),
):
    """Start a refinement of an existing screen."""
    project_dir = _find_project_dir()
    if not project_dir:
        return HTMLResponse(
            '<article><p>No active project.</p></article>', status_code=500,
        )
    spec_path = _screens_dir(project_dir) / f"{name}.json"
    if not spec_path.exists():
        return HTMLResponse(
            f'<article><p>Screen "{name}" not found.</p></article>', status_code=404,
        )
    if len(_active_generations) >= 2:
        return HTMLResponse(
            '<article><p>Too many concurrent tasks.</p></article>', status_code=429,
        )

    from tools.gui.hmi_generator import ALLOWED_MODELS, refine_screen
    selected_model = model if model in ALLOWED_MODELS else None

    gen_id = uuid.uuid4().hex[:8]
    _active_generations[gen_id] = refine_screen(
        prompt, project_dir, name, model=selected_model,
    )
    return HTMLResponse(
        f'<div id="refine-progress" hx-ext="sse"'
        f' sse-connect="/api/hmi/refine/{gen_id}/stream"'
        f' sse-swap="progress">'
        f'<p aria-busy="true">Starting refinement...</p></div>'
    )


@router.get("/api/hmi/refine/{gen_id}/stream")
async def hmi_refine_stream(gen_id: str):
    """SSE stream for refinement progress. On complete, reload current screen."""
    generator = _active_generations.get(gen_id)
    if not generator:
        return HTMLResponse("Refinement not found", status_code=404)

    async def _event_stream():
        try:
            async for event in generator:
                etype = event.get("type", "")
                if etype == "progress":
                    msg = event.get("message", "")
                    html = f'<p aria-busy="true">{msg}</p>'
                    yield f"event: progress\ndata: {html}\n\n"
                elif etype == "complete":
                    html = (
                        '<p>Screen updated! Reloading...</p>'
                        '<script>setTimeout(function(){window.location.reload()},600)</script>'
                    )
                    yield f"event: progress\ndata: {html}\n\n"
                elif etype == "error":
                    msg = event.get("message", "Unknown error")
                    html = f'<article><p>Error: {msg}</p></article>'
                    yield f"event: progress\ndata: {html}\n\n"
        finally:
            _active_generations.pop(gen_id, None)

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


@router.get("/api/hmi/catalog", response_class=HTMLResponse)
async def hmi_catalog(request: Request):
    """Return available OPC UA tags as an HTML table fragment."""
    project_dir = _find_project_dir()
    if not project_dir:
        return HTMLResponse(
            '<p>No active project found. Cannot load tag catalog.</p>'
        )

    endpoints = load_endpoints(project_dir)
    if not endpoints:
        return HTMLResponse('<p>No OPC UA endpoints configured.</p>')

    from tools.gui.opcua_client import get_opcua_client

    rows: list[str] = []
    total = 0
    per_plc: list[tuple[str, str, list]] = []
    for plc_name, endpoint in endpoints:
        try:
            opcua = get_opcua_client(endpoint)
            tags = await opcua.browse_tags()
        except Exception as exc:
            per_plc.append((plc_name, endpoint, []))
            rows.append(f'<p><strong>{plc_name}</strong> ({endpoint}): browse failed: {exc}</p>')
            continue
        per_plc.append((plc_name, endpoint, tags))
        total += len(tags)

    rows.insert(0, '<div class="catalog-header">'
                   f'<small>{len(endpoints)} PLCs &mdash; {total} tags total</small>'
                   '</div>')

    for plc_name, endpoint, tags in per_plc:
        if not tags:
            continue
        rows.append(f"<h4>{plc_name} <small>({endpoint})</small></h4>")
        groups: dict[str, list] = {}
        for t in tags:
            block = t.get("block", "Other")
            groups.setdefault(block, []).append(t)
        for block_name in sorted(groups):
            rows.append(f"<h5>{block_name}</h5>")
            rows.append('<table><thead><tr>')
            rows.append("<th>Tag</th><th>Type</th><th>Description</th>")
            rows.append("</tr></thead><tbody>")
            for t in sorted(groups[block_name], key=lambda x: x.get("name", "")):
                name = t.get("name", "")
                dtype = t.get("data_type", "?")
                desc = t.get("description", "")
                rows.append(
                    f"<tr><td><code>{plc_name}:{name}</code></td>"
                    f"<td>{dtype}</td>"
                    f"<td>{desc}</td></tr>"
                )
            rows.append("</tbody></table>")

    return HTMLResponse("\n".join(rows))


@router.post("/api/hmi/rebuild-catalog", response_class=HTMLResponse)
async def hmi_rebuild_catalog(request: Request):
    """Rebuild the tag catalog from SCL source files."""
    project_dir = _find_project_dir()
    if not project_dir:
        return HTMLResponse(
            '<article class="pico-background-red-500">'
            "<p>No active project found.</p></article>"
        )

    try:
        catalog = build_catalog(project_dir)
        save_catalog(catalog, project_dir)
        return HTMLResponse(
            f'<article><p>Catalog rebuilt: {len(catalog.tags)} tags discovered.</p></article>'
            '<script>setTimeout(()=>htmx.ajax("GET","/api/hmi/catalog",{target:"#catalog-container",swap:"innerHTML"}),500)</script>'
        )
    except Exception as exc:
        return HTMLResponse(
            f"<article><p>Error rebuilding catalog: {exc}</p></article>"
        )


@router.get("/api/hmi/opcua-catalog", response_class=HTMLResponse)
async def hmi_opcua_catalog(
    request: Request,
    endpoint: str = Query(""),
    filter: str = Query(""),
):
    """Browse OPC UA server and return tag catalog as HTML table."""
    from tools.gui.opcua_client import get_opcua_client

    # Explicit endpoint query: browse that single endpoint (unprefixed).
    if endpoint:
        try:
            opcua = get_opcua_client(endpoint)
            tags = await opcua.browse_tags(filter_block=filter)
        except Exception as exc:
            return HTMLResponse(f"<p>OPC UA browse failed: {exc}</p>")
        if not tags:
            return HTMLResponse("<p>No tags found on OPC UA server.</p>")
        rows: list[str] = []
        for t in sorted(tags, key=lambda x: x.get("name", "")):
            rows.append(
                f"<tr><td><code>{t.get('name','')}</code></td>"
                f"<td>{t.get('data_type','?')}</td>"
                f"<td>{t.get('block','')}</td>"
                f"<td>{t.get('description','')}</td></tr>"
            )
        html = (
            f"<p><strong>OPC UA tags from {endpoint}</strong> &mdash; {len(tags)} tags</p>"
            '<table role="grid"><thead><tr>'
            "<th>Tag</th><th>Type</th><th>Block</th><th>Description</th>"
            "</tr></thead><tbody>\n"
            + "\n".join(rows)
            + "\n</tbody></table>"
        )
        return HTMLResponse(html)

    # No endpoint given -> concat all project endpoints with PLC prefixes.
    project_dir = _find_project_dir()
    endpoints = load_endpoints(project_dir) if project_dir else []
    if not endpoints:
        return HTMLResponse("<p>No OPC UA endpoints configured.</p>")

    all_rows: list[str] = []
    total = 0
    for plc_name, ep in endpoints:
        try:
            opcua = get_opcua_client(ep)
            tags = await opcua.browse_tags(filter_block=filter)
        except Exception as exc:
            all_rows.append(
                f'<tr><td colspan="4"><em>{plc_name}: browse failed ({exc})</em></td></tr>'
            )
            continue
        total += len(tags)
        for t in sorted(tags, key=lambda x: x.get("name", "")):
            all_rows.append(
                f"<tr><td><code>{plc_name}:{t.get('name','')}</code></td>"
                f"<td>{t.get('data_type','?')}</td>"
                f"<td>{plc_name}/{t.get('block','')}</td>"
                f"<td>{t.get('description','')}</td></tr>"
            )

    html = (
        f"<p><strong>OPC UA tags from {len(endpoints)} PLCs</strong>"
        f" &mdash; {total} tags</p>"
        '<table role="grid"><thead><tr>'
        "<th>Tag</th><th>Type</th><th>Block</th><th>Description</th>"
        "</tr></thead><tbody>\n"
        + "\n".join(all_rows)
        + "\n</tbody></table>"
    )
    return HTMLResponse(html)


@router.delete("/api/hmi/screen/{name}", response_class=HTMLResponse)
async def hmi_delete_screen(name: str):
    """Delete a saved HMI screen."""
    project_dir = _find_project_dir()
    if not project_dir:
        return HTMLResponse("No active project", status_code=500)
    spec_path = _screens_dir(project_dir) / f"{name}.json"
    if not spec_path.exists():
        return HTMLResponse(f"Screen '{name}' not found", status_code=404)
    spec_path.unlink()
    # Return empty response — JS will handle redirect
    return HTMLResponse("")


@router.put("/api/hmi/screen/{name}/layout", response_class=HTMLResponse)
async def hmi_update_layout(request: Request, name: str):
    """Update widget order and column spans for a screen."""
    project_dir = _find_project_dir()
    if not project_dir:
        return HTMLResponse("No active project", status_code=500)
    spec_path = _screens_dir(project_dir) / f"{name}.json"
    if not spec_path.exists():
        return HTMLResponse(f"Screen '{name}' not found", status_code=404)

    body = await request.json()
    spec = ScreenSpec.model_validate_json(spec_path.read_text(encoding="utf-8-sig"))

    # body is {"widgets": [{"id": "w1", "columns": 2}, ...]} — reorder + resize
    widget_order = body.get("widgets", [])
    if widget_order:
        id_to_widget = {w.id: w for w in spec.widgets}
        new_widgets = []
        for item in widget_order:
            wid = item.get("id")
            if wid in id_to_widget:
                w = id_to_widget[wid]
                if "columns" in item:
                    w.columns = max(1, min(4, item["columns"]))
                new_widgets.append(w)
        # Keep any widgets not mentioned (shouldn't happen, but safe)
        mentioned = {item.get("id") for item in widget_order}
        for w in spec.widgets:
            if w.id not in mentioned:
                new_widgets.append(w)
        spec.widgets = new_widgets

    if "columns" in body:
        spec.columns = max(1, min(4, body["columns"]))

    spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    return HTMLResponse("OK")


@router.get("/api/hmi/opcua-status", response_class=HTMLResponse)
async def hmi_opcua_status(request: Request):
    """Check OPC UA connection status and return a status indicator fragment."""
    project_dir = _find_project_dir()
    if not project_dir:
        return HTMLResponse(
            '<span class="conn-status disconnected" title="No project">●</span>'
        )

    endpoints = load_endpoints(project_dir)
    if not endpoints:
        return HTMLResponse(
            '<span class="conn-status disconnected" title="No endpoints configured">●</span>'
        )

    import asyncio as _asyncio
    from tools.gui.opcua_client import get_opcua_client

    async def _probe(ep: str) -> bool:
        try:
            c = get_opcua_client(ep)
            await c.read_tags([])
            return True
        except Exception:
            return False

    results = await _asyncio.gather(*[_probe(ep) for _, ep in endpoints])
    up = sum(1 for r in results if r)
    total = len(endpoints)

    if up == total:
        cls = "connected"
        title = f"All {total} PLCs connected"
    elif up == 0:
        cls = "disconnected"
        title = f"All {total} PLCs disconnected"
    else:
        cls = "partial"
        down = [name for (name, _), ok in zip(endpoints, results) if not ok]
        title = f"{up}/{total} connected; down: {', '.join(down)}"

    return HTMLResponse(
        f'<span class="conn-status {cls}" title="{title}">●</span>'
    )


# ---------------------------------------------------------------------------
# Tag history API
# ---------------------------------------------------------------------------


@router.get("/api/hmi/history")
async def hmi_tag_history(
    tags: str = Query(""),
    last: int = Query(60),
):
    """Return tag value history as JSON for trend charts."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    result: dict[str, list] = {}
    for tag in tag_list:
        result[tag] = history.get(tag, last_n=last)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# OPC UA tag write API (for command widgets)
# ---------------------------------------------------------------------------


@router.post("/api/hmi/write-tag")
async def hmi_write_tag(request: Request):
    """Write a value to a PLC tag via OPC UA."""
    body = await request.json()
    tag = body.get("tag", "")
    value = body.get("value", "")
    endpoint = body.get("endpoint", "")

    if not tag:
        return JSONResponse({"ok": False, "message": "No tag specified"}, status_code=400)

    # Resolve endpoint from the tag's PLC prefix, or fall back to config.
    clean_tag = tag
    if not endpoint:
        project_dir = _find_project_dir()
        endpoints = load_endpoints(project_dir) if project_dir else []
        endpoint_map = {name: ep for name, ep in endpoints}
        prefix, remainder = split_prefixed_tag(tag)
        if prefix and prefix in endpoint_map:
            endpoint = endpoint_map[prefix]
            clean_tag = remainder
        elif len(endpoint_map) == 1:
            endpoint = next(iter(endpoint_map.values()))
            _, clean_tag = split_prefixed_tag(tag)

    if not endpoint:
        return JSONResponse(
            {"ok": False, "message": "No OPC UA endpoint configured"},
            status_code=500,
        )

    try:
        from tools.gui.opcua_client import get_opcua_client
        opcua = get_opcua_client(endpoint)
        success = await opcua.write_tag(clean_tag, value)
        if success:
            return JSONResponse({"ok": True, "message": f"Wrote {tag} = {value}"})
        else:
            return JSONResponse(
                {"ok": False, "message": f"Failed to write {tag}"},
                status_code=500,
            )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "message": f"Write error: {exc}"},
            status_code=500,
        )


@router.post("/api/hmi/alarm-ack")
async def hmi_alarm_ack(request: Request):
    """Acknowledge an active alarm."""
    body = await request.json()
    state_key = body.get("state_key", "")
    if not state_key or state_key not in _alarm_states:
        return JSONResponse({"ok": False, "message": "Unknown alarm"}, status_code=404)
    entry = _alarm_states[state_key]
    if not entry.get("active"):
        return JSONResponse({"ok": False, "message": "Alarm is not active"}, status_code=400)
    entry["acked"] = True
    entry["ack_time"] = int(time.time() * 1000)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Farm-wide overview dashboard
# ---------------------------------------------------------------------------


def _overview_tags(turbines: list[str]) -> list[str]:
    tags = [
        'SCADA:"HMI_DB".Farm.rTotalPower_MW',
        'SCADA:"HMI_DB".Farm.nTurbinesRunning',
        'SCADA:"HMI_DB".Farm.nTurbinesFaulted',
        'SCADA:"HMI_DB".Farm.rAvailability_Pct',
        'SCADA:"HMI_DB".Grid.rGridPrice_EUR_MWh',
        'MET01:"HMI_DB".Wind.rWindSpeed_90m',
        'MET01:"HMI_DB".Wind.rWindDir_90m_deg',
        'MET01:"HMI_DB".Sea.rWaveHeight_m',
        'MET01:"HMI_DB".Weather.rAirTemp_C',
        'SUB01:"HMI_DB".Export.rExportPower_MW',
        'SUB01:"HMI_DB".Export.rGridFreq_Hz',
    ]
    for t in turbines:
        tags.append(f'{t}:"HMI_DB".Power.rActivePower_kW')
        tags.append(f'{t}:"HMI_DB".Turbine.xRunning_Sts')
        tags.append(f'{t}:"HMI_DB".Turbine.xFault_Sts')
    return tags


async def _read_multi_plc(
    project_dir: Path, tag_list: list[str],
) -> dict[str, object]:
    """Read a batch of PLC-prefixed tags, fanning out to each endpoint."""
    import asyncio as _asyncio

    from tools.gui.opcua_client import get_opcua_client

    endpoints = load_endpoints(project_dir)
    endpoint_map = {name: ep for name, ep in endpoints}
    if not endpoint_map:
        return {t: None for t in tag_list}

    default_plc = next(iter(endpoint_map)) if len(endpoint_map) == 1 else None
    groups = group_tags_by_plc(tag_list, default_plc=default_plc)

    async def _read_group(plc, ptags):
        ep = endpoint_map.get(plc)
        if not ep:
            return {t: None for t in ptags}
        clean_map = {}
        for p in ptags:
            _, rem = split_prefixed_tag(p)
            clean_map[p] = rem
        try:
            opcua = get_opcua_client(ep)
            raw = await opcua.read_tags(list(clean_map.values()))
            return {p: raw.get(c) for p, c in clean_map.items()}
        except Exception:
            return {t: None for t in ptags}

    result: dict[str, object] = {}
    for u in groups.get(None, []):
        result[u] = None
    tasks = [_read_group(p, t) for p, t in groups.items() if p]
    if tasks:
        for g in await _asyncio.gather(*tasks):
            result.update(g)
    return result


def _fmt(raw, spec: str = ".1f") -> str:
    if raw is None:
        return "\u2014"
    try:
        return format(float(raw), spec)
    except (ValueError, TypeError):
        return str(raw)


def _as_bool(raw) -> bool:
    if raw is None:
        return False
    return str(raw).lower() in ("true", "1", "1.0")


def _wind_compass(deg) -> str:
    if deg is None:
        return ""
    try:
        d = float(deg) % 360
    except (ValueError, TypeError):
        return ""
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((d + 22.5) // 45) % 8]


@router.get("/api/hmi/overview", response_class=HTMLResponse)
async def hmi_farm_overview(request: Request):
    """Render the live farm overview dashboard fragment."""
    project_dir = _find_project_dir()
    if not project_dir:
        return HTMLResponse(
            '<div class="overview-empty">No active project.</div>'
        )

    endpoints = load_endpoints(project_dir)
    plc_names = [n for n, _ in endpoints]
    turbines = [n for n in plc_names if n.startswith("WTG")]

    if not turbines and "SCADA" not in plc_names:
        return HTMLResponse(
            '<div class="overview-empty">Farm overview requires WTGxx/SCADA/MET01/SUB01 endpoints.</div>'
        )

    tags = _overview_tags(turbines)
    values = await _read_multi_plc(project_dir, tags)

    v = lambda key: values.get(key)  # noqa: E731

    total_mw = v('SCADA:"HMI_DB".Farm.rTotalPower_MW')
    running = v('SCADA:"HMI_DB".Farm.nTurbinesRunning')
    faulted = v('SCADA:"HMI_DB".Farm.nTurbinesFaulted')
    avail = v('SCADA:"HMI_DB".Farm.rAvailability_Pct')
    grid_price = v('SCADA:"HMI_DB".Grid.rGridPrice_EUR_MWh')
    wind = v('MET01:"HMI_DB".Wind.rWindSpeed_90m')
    wind_dir = v('MET01:"HMI_DB".Wind.rWindDir_90m_deg')
    waves = v('MET01:"HMI_DB".Sea.rWaveHeight_m')
    air_temp = v('MET01:"HMI_DB".Weather.rAirTemp_C')
    grid_freq = v('SUB01:"HMI_DB".Export.rGridFreq_Hz')

    n_turb = len(turbines)
    running_int = int(running) if running is not None else 0
    avail_pct = _fmt(avail, ".0f")
    compass = _wind_compass(wind_dir)

    # Hero tiles
    hero = f"""
    <div class="ovw-hero">
      <div class="ovw-tile ovw-tile-primary">
        <div class="ovw-tile-val">{_fmt(total_mw, ".1f")}</div>
        <div class="ovw-tile-unit">MW</div>
        <div class="ovw-tile-label">FARM OUTPUT</div>
        <div class="ovw-tile-sub">{avail_pct}% AVAILABLE</div>
      </div>
      <div class="ovw-tile">
        <div class="ovw-tile-val">{_fmt(wind, ".1f")}</div>
        <div class="ovw-tile-unit">m/s</div>
        <div class="ovw-tile-label">WIND @ 90m</div>
        <div class="ovw-tile-sub">{compass} &middot; {_fmt(wind_dir, ".0f")}&deg;</div>
      </div>
      <div class="ovw-tile">
        <div class="ovw-tile-val">{running_int}<span class="ovw-tile-frac">/{n_turb}</span></div>
        <div class="ovw-tile-unit">&nbsp;</div>
        <div class="ovw-tile-label">TURBINES ONLINE</div>
        <div class="ovw-tile-sub">{_fmt(faulted, ".0f")} FAULTED</div>
      </div>
      <div class="ovw-tile">
        <div class="ovw-tile-val">{_fmt(grid_price, ".0f")}</div>
        <div class="ovw-tile-unit">&euro;/MWh</div>
        <div class="ovw-tile-label">GRID PRICE</div>
        <div class="ovw-tile-sub">{_fmt(grid_freq, ".2f")} Hz</div>
      </div>
    </div>
    """

    # Turbine strip
    turbine_cards: list[str] = []
    for tname in turbines:
        p = v(f'{tname}:"HMI_DB".Power.rActivePower_kW')
        running_t = _as_bool(v(f'{tname}:"HMI_DB".Turbine.xRunning_Sts'))
        fault_t = _as_bool(v(f'{tname}:"HMI_DB".Turbine.xFault_Sts'))
        if fault_t:
            state_cls, state = "fault", "FAULT"
        elif running_t:
            state_cls, state = "running", "RUNNING"
        else:
            state_cls, state = "idle", "IDLE"
        pw_mw = None
        try:
            pw_mw = float(p) / 1000.0 if p is not None else None
        except (ValueError, TypeError):
            pw_mw = None
        # normalize to 5 MW nameplate for bar fill
        pct = 0.0
        if pw_mw is not None:
            pct = max(0.0, min(100.0, pw_mw / 5.0 * 100.0))
        turbine_cards.append(f"""
      <div class="ovw-turbine ovw-turbine-{state_cls}">
        <div class="ovw-turbine-head">
          <span class="ovw-turbine-name">{tname}</span>
          <span class="ovw-turbine-dot"></span>
        </div>
        <div class="ovw-turbine-power">{_fmt(pw_mw, ".2f")} <span>MW</span></div>
        <div class="ovw-turbine-bar"><div class="ovw-turbine-fill" style="width:{pct:.0f}%"></div></div>
        <div class="ovw-turbine-state">{state}</div>
      </div>""")

    turbine_strip = (
        '<div class="ovw-turbines">' + "".join(turbine_cards) + "</div>"
        if turbine_cards else ""
    )

    # Environment footer
    footer = f"""
    <div class="ovw-footer">
      <span class="ovw-chip"><span class="ovw-chip-label">WAVES</span> {_fmt(waves, ".1f")} m</span>
      <span class="ovw-chip"><span class="ovw-chip-label">AIR</span> {_fmt(air_temp, ".1f")} &deg;C</span>
      <span class="ovw-chip"><span class="ovw-chip-label">EXPORT</span> {_fmt(v('SUB01:"HMI_DB".Export.rExportPower_MW'), ".1f")} MW</span>
    </div>
    """

    return HTMLResponse(hero + turbine_strip + footer)
