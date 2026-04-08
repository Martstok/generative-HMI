"""HMI screen generator — invokes Claude Code CLI to produce ScreenSpec JSON."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import AsyncIterator

from tools.gui.hmi_models import ScreenSpec
from tools.gui.project_endpoints import load_endpoints
from tools.gui.tag_catalog import load_catalog

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STOP_WORDS: set[str] = {
    "show", "me", "all", "the", "and", "with", "for", "a", "an",
    "of", "in", "on", "my", "give", "display", "create", "generate",
}

_MAX_CONCURRENT = 2
_TIMEOUT_S = 120

ALLOWED_MODELS: frozenset[str] = frozenset({
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5-20251001",
})

_MODEL_LABELS: dict[str, str] = {
    "claude-sonnet-4-6": "Sonnet",
    "claude-opus-4-6": "Opus",
    "claude-haiku-4-5-20251001": "Haiku",
}


def _model_label(model: str | None) -> str:
    if not model:
        return "default"
    return _MODEL_LABELS.get(model, model)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(prompt: str, screens_dir: Path) -> str:
    """Derive a filesystem-safe slug from a user prompt."""
    lower = prompt.lower()
    cleaned = re.sub(r"[^a-z0-9 ]", " ", lower)
    words = [w for w in cleaned.split() if w not in _STOP_WORDS and len(w) >= 2]
    slug = "-".join(words[:4]) or "screen"

    # Ensure uniqueness within screens_dir
    if (screens_dir / f"{slug}.json").exists():
        n = 2
        while (screens_dir / f"{slug}-{n}.json").exists():
            n += 1
        slug = f"{slug}-{n}"

    return slug


def _build_system_prompt(tags_text: str, endpoints: list[tuple[str, str]]) -> str:
    """Build the system prompt that instructs Claude to produce a ScreenSpec."""
    schema_json = json.dumps(ScreenSpec.model_json_schema(), indent=2)
    plc_names = ", ".join(name for name, _ in endpoints) or "(none)"
    return f"""\
You are an HMI screen generator for a multi-PLC SCADA control center.

Return ONLY valid JSON matching the ScreenSpec schema below. No markdown fences, \
no explanation, no extra text — just the raw JSON object.

## ScreenSpec JSON Schema

{schema_json}

## Widget types

- gauge: Half-circle gauge for a single numeric value. Needs min_value and max_value. Best for 1 tag per widget.
- value_card: Shows one or more numeric values with labels and units. Good for grouping related readings.
- bar_chart: Horizontal bars comparing multiple values. Needs min_value and max_value.
- status: Boolean status indicators (colored dots). Use for alarm/fault/enable flags.
- table: Tabular display of multiple tag values. Good for configuration parameters or limits.
- trend_chart: Time-series line chart showing value history. Good for 1-4 numeric tags that change over time. Set min_value/max_value for Y-axis, or omit for auto-scale. Use columns=2 or more.
- alarm_list: Live alarm/fault log with severity indicators. Use for boolean alarm/fault tags. Set severity to "info", "warning", or "critical".
- command: Buttons that write values to PLC tags. Use for boolean command tags (start/stop, enable/disable). Set write_value to the value to write (default "true"). Tags should be command tags ending in _Cmd.
- schematic: Live tag overlays on a predefined process/electrical diagram. Pick from the library (set the `diagram` field on the widget):

  diagram="farm_sld" — Whole-farm electrical single-line. columns=4.
    Available anchors: wtg1_power, wtg2_power, wtg3_power, wtg4_power,
                       wtg1_status, wtg2_status, wtg3_status, wtg4_status,
                       bus_voltage, transformer_oil_temp,
                       export_mw, export_current, grid_freq, export_breaker
    Bind tags from SCADA / SUB01 / WTG01..WTG04.

  diagram="substation_sld" — Substation double-bus + transformer. columns=4.
    Available anchors: incomer_a, incomer_b, busbar_a, busbar_b, tie_breaker,
                       transformer_primary_v, transformer_secondary_v,
                       oil_temp, winding_temp, tap_position,
                       export_breaker, export_current
    Bind tags from SUB01 only.

  diagram="turbine_drivetrain" — Single turbine mechanical/electrical chain. columns=4.
    Available anchors: wind_speed, rotor_rpm, pitch, gearbox_oil_temp,
                       gearbox_vibration, gen_rpm, gen_power,
                       nacelle_temp, yaw_position, grid_power
    Bind tags from ONE WTGxx PLC (choose the turbine the user asks about; default WTG01).

For every tag in a schematic widget, you MUST set `anchor` to one of the anchor names above. Unused anchors will show "—".

## Available tags

{tags_text}

## Multi-PLC tag naming

This project has multiple PLCs ({plc_names}). Each tag is prefixed with its PLC name followed by a colon:
  WTG01:"HMI_DB".Power.rActivePower_kW

When the user asks for screens spanning multiple PLCs (e.g. "fleet overview", "all turbines"), use tags from each relevant PLC. When they ask for a specific system (e.g. "substation panel"), use tags from only that PLC.

- Leave opcua_endpoint empty in the output — the HMI resolves endpoints per tag via the prefix.

## Rules

- Use only tags from the Available Tags list, including their PLC prefix.
- Every widget needs a unique id: w1, w2, w3...
- Match widget type to the data (gauges for measurements, status for booleans, etc.).
- Set opcua_endpoint to "".
- Set poll_interval_ms to 1000.
- Set columns to 4 for the grid.
- Choose a descriptive title and brief description.\
"""


async def _load_tags_text(
    project_dir: Path,
) -> tuple[str, list[tuple[str, str]]]:
    """Load available tags for every configured PLC endpoint.

    Returns:
        ``(tags_text, endpoints)`` where *tags_text* is a PLC-grouped listing
        with prefixed tag names and *endpoints* is ``[(plc_name, url), ...]``.
    """
    from loguru import logger as _log

    endpoints = load_endpoints(project_dir)

    if endpoints:
        from tools.gui.opcua_client import get_opcua_client

        sections: list[str] = []
        for plc_name, endpoint in endpoints:
            try:
                client = get_opcua_client(endpoint)
                tags = await client.browse_tags()
            except Exception as exc:
                _log.warning("Browse failed for {} ({}): {}", plc_name, endpoint, exc)
                continue
            if not tags:
                continue
            sections.append(f"## PLC: {plc_name}")
            for t in tags:
                sections.append(
                    f'{plc_name}:{t["name"]} | {t["data_type"]} | {t.get("description", "")}'
                )
            sections.append("")
        if sections:
            return "\n".join(sections), endpoints

    # Fall back to the static tag catalog (unprefixed, legacy behaviour)
    catalog = load_catalog(project_dir)
    if catalog and catalog.tags:
        lines = [f"{t.name} | {t.data_type} | {t.description}" for t in catalog.tags]
        return "\n".join(lines), endpoints

    return "No tags available", endpoints


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_refine_system_prompt(
    current_spec_json: str, tags_text: str, endpoints: list[tuple[str, str]],
) -> str:
    schema_json = json.dumps(ScreenSpec.model_json_schema(), indent=2)
    plc_names = ", ".join(n for n, _ in endpoints) if endpoints else ""
    return f"""\
You are modifying an existing HMI screen. Apply the user's request and return the FULL updated ScreenSpec JSON. Keep unchanged widgets intact \u2014 preserve their ids, columns, and tag bindings. Only modify what the user asked for.

Return ONLY valid JSON matching the ScreenSpec schema. No markdown fences, no explanation.

## ScreenSpec JSON Schema

{schema_json}

## Current ScreenSpec

{current_spec_json}

## Available PLCs
{plc_names}

## Available tags (prefix with PLC name + ':')

{tags_text}

## Rules
- Return the COMPLETE updated ScreenSpec \u2014 do not omit unchanged widgets.
- Keep existing widget ids stable (w1, w2, ...) where possible.
- If adding a new widget, give it the next available id.
- Use only tags from the Available tags list with the PLC prefix.
- Leave opcua_endpoint empty.
"""


async def refine_screen(
    refinement_prompt: str, project_dir: Path, spec_name: str,
    model: str | None = None,
) -> AsyncIterator[dict]:
    """Async generator that refines an existing screen in-place."""
    screens_dir = project_dir / "hmi_screens"
    spec_path = screens_dir / f"{spec_name}.json"
    if not spec_path.exists():
        yield {"type": "error", "message": f"Screen '{spec_name}' not found."}
        return

    try:
        current_spec_json = spec_path.read_text(encoding="utf-8-sig")
        current_spec = ScreenSpec.model_validate_json(current_spec_json)
    except Exception as exc:
        yield {"type": "error", "message": f"Failed to load current spec: {exc}"}
        return

    yield {"type": "progress", "message": f"Preparing refinement (screen: {spec_name})..."}

    tags_text, endpoints = await _load_tags_text(project_dir)
    system_prompt = _build_refine_system_prompt(current_spec_json, tags_text, endpoints)

    model_label = _model_label(model)
    yield {"type": "progress", "message": f"Calling Claude Code ({model_label})..."}

    user_prompt = f"Apply this change: {refinement_prompt}"

    cli_args = [
        "claude", "-p", user_prompt,
        "--output-format", "json",
        "--max-turns", "1",
        "--system-prompt", system_prompt,
    ]
    if model:
        cli_args.extend(["--model", model])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cli_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        yield {"type": "error", "message": "Claude Code CLI not found. Is it installed?"}
        return

    yield {"type": "progress", "message": "Claude is refining your screen..."}

    try:
        stdout_data, stderr_data = await asyncio.wait_for(
            proc.communicate(), timeout=_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        proc.kill()
        yield {"type": "error", "message": f"Refinement timed out after {_TIMEOUT_S}s"}
        return

    stdout_text = stdout_data.decode("utf-8", errors="replace")

    result_text = ""
    try:
        output = json.loads(stdout_text)
        if output.get("is_error"):
            yield {
                "type": "error",
                "message": f"Claude CLI error: {output.get('result', 'unknown')}",
            }
            return
        result_text = output.get("result", "")
    except json.JSONDecodeError:
        result_text = stdout_text.strip()

    if not result_text:
        stderr_text = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""
        yield {
            "type": "error",
            "message": f"Claude returned empty result. stderr: {stderr_text[:500]}",
        }
        return

    yield {"type": "progress", "message": "Validating updated screen..."}

    json_text = result_text.strip()
    if json_text.startswith("```"):
        lines = json_text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        json_text = "\n".join(lines).strip()

    if not json_text.startswith("{"):
        start = json_text.find("{")
        end = json_text.rfind("}") + 1
        if start >= 0 and end > start:
            json_text = json_text[start:end]

    try:
        raw = json.loads(json_text)
    except json.JSONDecodeError as exc:
        yield {
            "type": "error",
            "message": f"Claude returned invalid JSON: {exc}\n\nRaw (first 500 chars):\n{json_text[:500]}",
        }
        return

    # Strip unknown fields / normalise widget types
    if isinstance(raw, dict) and "widgets" in raw:
        for w in raw["widgets"]:
            if isinstance(w, dict) and "type" in w:
                type_fixes = {
                    "trend": "trend_chart",
                    "chart": "trend_chart",
                    "line_chart": "trend_chart",
                    "alarm": "alarm_list",
                    "alarms": "alarm_list",
                    "button": "command",
                    "buttons": "command",
                    "sld": "schematic",
                    "schematic_diagram": "schematic",
                    "diagram": "schematic",
                    "pid": "schematic",
                    "p&id": "schematic",
                }
                w["type"] = type_fixes.get(w["type"], w["type"])

    try:
        spec = ScreenSpec.model_validate(raw)
    except Exception as exc:
        yield {
            "type": "error",
            "message": f"Invalid screen spec: {exc}\n\nParsed JSON keys: {list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__}",
        }
        return

    # Post-processing: append the refinement to the prompt history
    old_prompt = current_spec.prompt or ""
    if old_prompt:
        spec.prompt = f"{old_prompt} | REFINE: {refinement_prompt}"
    else:
        spec.prompt = f"REFINE: {refinement_prompt}"
    if spec.poll_interval_ms < 500:
        spec.poll_interval_ms = 1000

    for i, widget in enumerate(spec.widgets):
        widget.id = f"w{i + 1}"

    # Save back to the SAME path (overwrite)
    spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")

    yield {"type": "complete", "screen_name": spec_name}


async def generate_screen(
    prompt: str, project_dir: Path, model: str | None = None,
) -> AsyncIterator[dict]:
    """Async generator that yields progress/error/complete events.

    Each yielded dict has a ``"type"`` key (``"progress"``, ``"error"``, or
    ``"complete"``) plus type-specific fields.
    """
    screens_dir = project_dir / "hmi_screens"
    screens_dir.mkdir(exist_ok=True)

    slug = _slugify(prompt, screens_dir)

    yield {"type": "progress", "message": f"Preparing prompt (screen: {slug})..."}

    tags_text, endpoints = await _load_tags_text(project_dir)
    system_prompt = _build_system_prompt(tags_text, endpoints)

    model_label = _model_label(model)
    yield {"type": "progress", "message": f"Calling Claude Code ({model_label})..."}

    user_prompt = f"Generate an HMI screen for: {prompt}"

    cli_args = [
        "claude", "-p", user_prompt,
        "--output-format", "json",
        "--max-turns", "1",
        "--system-prompt", system_prompt,
    ]
    if model:
        cli_args.extend(["--model", model])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cli_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        yield {"type": "error", "message": "Claude Code CLI not found. Is it installed?"}
        return

    yield {"type": "progress", "message": "Claude is generating your screen..."}

    # Read all output at once with a timeout
    result_text = ""
    try:
        stdout_data, stderr_data = await asyncio.wait_for(
            proc.communicate(), timeout=_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        proc.kill()
        yield {"type": "error", "message": f"Generation timed out after {_TIMEOUT_S}s"}
        return

    stdout_text = stdout_data.decode("utf-8", errors="replace")

    # --output-format json returns a single JSON object with a "result" field
    try:
        output = json.loads(stdout_text)
        if output.get("is_error"):
            yield {
                "type": "error",
                "message": f"Claude CLI error: {output.get('result', 'unknown')}",
            }
            return
        result_text = output.get("result", "")
    except json.JSONDecodeError:
        # Fallback: treat entire stdout as the result
        result_text = stdout_text.strip()

    if not result_text:
        stderr_text = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""
        yield {
            "type": "error",
            "message": f"Claude returned empty result. stderr: {stderr_text[:500]}",
        }
        return

    yield {"type": "progress", "message": "Validating screen specification..."}

    # Extract JSON from result_text (Claude may wrap it in markdown fences)
    json_text = result_text.strip()
    if json_text.startswith("```"):
        lines = json_text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        json_text = "\n".join(lines).strip()

    # Try to find JSON object if there's extra text around it
    if not json_text.startswith("{"):
        start = json_text.find("{")
        end = json_text.rfind("}") + 1
        if start >= 0 and end > start:
            json_text = json_text[start:end]

    # Two-step parse: first raw JSON, then Pydantic validation.
    # This gives better error messages and lets us fix common issues.
    try:
        raw = json.loads(json_text)
    except json.JSONDecodeError as exc:
        yield {
            "type": "error",
            "message": f"Claude returned invalid JSON: {exc}\n\nRaw (first 500 chars):\n{json_text[:500]}",
        }
        return

    # Strip unknown fields that Claude might hallucinate
    if isinstance(raw, dict) and "widgets" in raw:
        for w in raw["widgets"]:
            if isinstance(w, dict) and "type" in w:
                # Normalise common type mistakes
                type_fixes = {
                    "trend": "trend_chart",
                    "chart": "trend_chart",
                    "line_chart": "trend_chart",
                    "alarm": "alarm_list",
                    "alarms": "alarm_list",
                    "button": "command",
                    "buttons": "command",
                    "sld": "schematic",
                    "schematic_diagram": "schematic",
                    "diagram": "schematic",
                    "pid": "schematic",
                    "p&id": "schematic",
                }
                w["type"] = type_fixes.get(w["type"], w["type"])

    try:
        spec = ScreenSpec.model_validate(raw)
    except Exception as exc:
        yield {
            "type": "error",
            "message": f"Invalid screen spec: {exc}\n\nParsed JSON keys: {list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__}",
        }
        return

    # Post-processing
    spec.prompt = prompt
    if spec.poll_interval_ms < 500:
        spec.poll_interval_ms = 1000

    # Ensure unique widget IDs
    for i, widget in enumerate(spec.widgets):
        widget.id = f"w{i + 1}"

    # Save
    spec_path = screens_dir / f"{slug}.json"
    spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")

    yield {"type": "complete", "screen_name": slug}
