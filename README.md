# Generative HMI

AI-generated HMI screens for industrial control. Describe the screen you want
in plain language — an LLM designs it, and the app renders it live against
real OPC UA process data.

> *"Show me an overview of all turbines with power output, rotor speed and
> active alarms"*

…becomes a polling operator screen with gauges, trend charts, alarm lists and
an SVG single-line diagram — every value bound to a live tag.

## How it works

```
 prompt ──► Claude (headless CLI) ──► ScreenSpec JSON ──► renderer ──► live HMI
                    │                        │                            │
             tag catalog +            Pydantic-validated,          htmx + SSE,
             widget schema             saved to disk            OPC UA polling
```

1. The user types a natural-language prompt in the web UI and picks a model
   (Haiku / Sonnet / Opus).
2. `hmi_generator.py` invokes the Claude Code CLI headlessly with the
   project's **tag catalog** and the `ScreenSpec` JSON schema
   (`hmi_models.py`).
3. The returned spec is validated with Pydantic and saved to disk — screens
   are reproducible artifacts you can revisit and refine, not one-off chat
   output. Generation history is kept per screen.
4. `hmi_renderer.py` renders the spec as an htmx page. Tag values stream in
   over OPC UA (`asyncua`) at the spec's poll interval; command widgets write
   back to the PLC.

## Widget set

| Widget | Notes |
|---|---|
| `value_card`, `gauge`, `bar_chart`, `status`, `table` | Standard process values |
| `trend_chart` | Client-side rolling trend |
| `alarm_list` | Severity-classified (info / warning / critical) |
| `command` | Writes a value to the PLC on click |
| `schematic` | SVG diagrams with **anchor-bound live values** — tags pinned to fixed positions inside the drawing |

Built-in schematics: wind-farm single-line diagram, substation single-line
diagram, turbine drivetrain.

## Demo project: Nordsea Alpha

A simulated offshore wind farm SCADA control center. `mock_opcua_server.py`
hosts **seven independent OPC UA servers** in one process — four turbines
(`WTG01–04`), a substation (`SUB01`), a met mast (`MET01`) and a plant master
(`SCADA`) — driven by a shared simulation engine so values stay physically
coherent across servers (wind speed at the met mast matches turbine output,
substation power matches the sum of the turbines, and so on).

## Quick start

Requires Python 3.11+ and the [Claude Code CLI](https://claude.com/claude-code)
on `PATH` (screen generation runs on your Claude subscription — no API key).

```bash
pip install -e .

# Terminal 1 — start the simulated wind farm (7 OPC UA servers on :4841–:4847)
python -m tools.gui.mock_opcua_server

# Terminal 2 — launch the web app (opens a browser at :8100)
generative-hmi
```

Open the **Nordsea Alpha** project, type a prompt, and generate a screen.
Point `projects/<name>/project.json` at your own OPC UA endpoints to run it
against real equipment.

## Status

Personal R&D project exploring where generative AI genuinely helps in
industrial HMI work: turning intent into a validated, reviewable screen spec —
while rendering, data binding and writes stay deterministic code. Not
production software; no authentication, and command writes are enabled by
default.
