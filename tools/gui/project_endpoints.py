"""Helpers for resolving multiple OPC UA endpoints from a project config."""

from __future__ import annotations

import json
import re
from pathlib import Path

_PREFIX_RE = re.compile(r'^([A-Z0-9_]+):(".*)')


def load_endpoints(project_dir: Path) -> list[tuple[str, str]]:
    """Return ``[(plc_name, endpoint_url), ...]`` for a project.

    Supports both the modern ``opcua_endpoints`` list and legacy
    ``opcua_endpoint`` string. Returns ``[]`` if neither is configured.
    """
    pj_path = project_dir / "project.json"
    if not pj_path.exists():
        return []
    try:
        pj = json.loads(pj_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return []

    endpoints = pj.get("opcua_endpoints")
    if isinstance(endpoints, list) and endpoints:
        result: list[tuple[str, str]] = []
        for item in endpoints:
            if isinstance(item, dict):
                name = item.get("name", "")
                ep = item.get("endpoint", "")
                if name and ep:
                    result.append((name, ep))
        if result:
            return result

    legacy = pj.get("opcua_endpoint", "")
    if legacy:
        return [("PLC", legacy)]
    return []


def split_prefixed_tag(tag: str) -> tuple[str | None, str]:
    """Split ``WTG02:"HMI_DB".Power.rActivePower_kW`` into (prefix, remainder).

    Returns ``(None, tag)`` when no PLC prefix is present.
    """
    m = _PREFIX_RE.match(tag)
    if m:
        return m.group(1), m.group(2)
    return None, tag


def group_tags_by_plc(
    tags: list[str], default_plc: str | None = None,
) -> dict[str | None, list[str]]:
    """Group a flat tag list by PLC prefix. Unprefixed tags go under default_plc."""
    groups: dict[str | None, list[str]] = {}
    for tag in tags:
        prefix, _ = split_prefixed_tag(tag)
        key = prefix if prefix is not None else default_plc
        groups.setdefault(key, []).append(tag)
    return groups
