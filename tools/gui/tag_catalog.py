"""Tag catalog builder — parses SCL/DB source files to discover PLC tags.

This module is fully project-agnostic. It scans whatever SCL files exist in a
project's ``src/`` directory and builds a catalog of tags that can be used for
HMI screen generation or tag browsing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from .hmi_models import TagCatalog, TagEntry

# ---------------------------------------------------------------------------
# Category classification — keyword → category
# ---------------------------------------------------------------------------

_CATEGORY_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)motor|drive|vfd"), "motor"),
    (re.compile(r"(?i)valve"), "valve"),
    (re.compile(r"(?i)sensor|analog|pressure|temp|level|flow"), "sensor"),
    (re.compile(r"(?i)hpu|hydraulic"), "hydraulic"),
    (re.compile(r"(?i)alarm|fault"), "alarm"),
]


def _classify(block_name: str, tag_name: str) -> str:
    """Return a heuristic category based on block and tag name keywords."""
    combined = f"{block_name} {tag_name}"
    for pattern, category in _CATEGORY_RULES:
        if pattern.search(combined):
            return category
    return "general"


# ---------------------------------------------------------------------------
# SCL variable-declaration parser
# ---------------------------------------------------------------------------

# Matches a variable declaration line such as:
#   rSetpoint : Real;   // Target speed setpoint
#   rSetpoint : Real := 0.0;   // with default
#   arValues : Array[0..9] of Real;
#   stParams : "UDT_ScaleParams";
#   WinchOn_HmiCmd { S7_SetPoint := 'False'} : "HmiCommand";
_VAR_DECL_RE = re.compile(
    r"""
    ^\s*
    (?P<name>"[^"]+"|[A-Za-z_][A-Za-z0-9_/]*)   # variable name (may be quoted)
    (?:\s*\{[^}]*\})?                              # optional attributes like { S7_SetPoint := 'False'}
    \s*:\s*
    (?P<type>.+?)                                  # data type (greedy-lazy up to ; or :=)
    \s*(?::=\s*[^;]*)?                             # optional initialiser
    \s*;
    \s*(?://\s*(?P<comment>.*))?                   # optional inline comment
    $
    """,
    re.VERBOSE,
)

# Section headers inside blocks
_SECTION_START_RE = re.compile(
    r"^\s*VAR(?:_INPUT|_OUTPUT|_IN_OUT|_TEMP)?\s*(?:RETAIN)?\s*$", re.IGNORECASE
)
_SECTION_END_RE = re.compile(r"^\s*END_VAR\b", re.IGNORECASE)

# Nested STRUCT boundaries (inside VAR sections)
_STRUCT_OPEN_RE = re.compile(r":\s*Struct\s*(?:;?\s*//.*)?$", re.IGNORECASE)
_STRUCT_CLOSE_RE = re.compile(r"^\s*END_STRUCT\s*;", re.IGNORECASE)


class _VarInfo(NamedTuple):
    name: str
    data_type: str
    comment: str
    section: str  # "VAR_INPUT", "VAR_OUTPUT", etc.


def _strip_quotes(s: str) -> str:
    """Remove surrounding double-quotes from a name if present."""
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    return s


def _parse_var_sections(lines: list[str]) -> list[_VarInfo]:
    """Extract all variable declarations from VAR..END_VAR sections."""
    results: list[_VarInfo] = []
    in_section = False
    section_name = ""
    struct_depth = 0
    struct_prefix: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Detect section start
        if not in_section:
            m = re.match(
                r"^\s*(VAR(?:_INPUT|_OUTPUT|_IN_OUT|_TEMP)?)\s*(?:RETAIN)?\s*$",
                stripped,
                re.IGNORECASE,
            )
            if m:
                in_section = True
                section_name = m.group(1).upper()
                struct_depth = 0
                struct_prefix = []
                continue
        else:
            # Detect section end
            if _SECTION_END_RE.match(stripped):
                in_section = False
                continue

            # Track nested structs — parse members with qualified prefix
            if _STRUCT_OPEN_RE.search(stripped):
                # Extract struct variable name (VAR_DECL_RE won't match — no semicolon)
                sm = re.match(
                    r'^\s*(?P<name>"[^"]+"|[A-Za-z_]\w*)\s*(?:\{[^}]*\})?\s*:\s*Struct',
                    stripped,
                    re.IGNORECASE,
                )
                if sm:
                    struct_var = _strip_quotes(sm.group("name"))
                    prefix = ".".join(struct_prefix + [struct_var])
                    # Extract optional trailing comment
                    cm = re.search(r"//\s*(.*?)\s*$", stripped)
                    vcomment = cm.group(1) if cm else ""
                    results.append(_VarInfo(prefix, "Struct", vcomment, section_name))
                    struct_prefix.append(struct_var)
                struct_depth += 1
                continue
            if _STRUCT_CLOSE_RE.match(stripped):
                struct_depth = max(0, struct_depth - 1)
                if struct_prefix:
                    struct_prefix.pop()
                continue

            # Try to match a variable declaration
            m = _VAR_DECL_RE.match(stripped)
            if m:
                vname = _strip_quotes(m.group("name"))
                vtype = m.group("type").strip()
                vcomment = (m.group("comment") or "").strip()
                if struct_prefix:
                    vname = ".".join(struct_prefix + [vname])
                results.append(_VarInfo(vname, vtype, vcomment, section_name))

    return results


# ---------------------------------------------------------------------------
# Block-type detection helpers
# ---------------------------------------------------------------------------

_FB_RE = re.compile(r'^\s*FUNCTION_BLOCK\s+"([^"]+)"', re.IGNORECASE)
_FC_RE = re.compile(r'^\s*FUNCTION\s+"([^"]+)"', re.IGNORECASE)
_UDT_RE = re.compile(r'^\s*TYPE\s+"([^"]+)"', re.IGNORECASE)


def _detect_scl_block(lines: list[str]) -> tuple[str, str]:
    """Return (block_type, block_name) for an SCL file.

    block_type is one of: "FB", "FC", "UDT", "unknown".
    """
    for line in lines[:5]:
        m = _FB_RE.match(line)
        if m:
            return ("FB", m.group(1))
        m = _FC_RE.match(line)
        if m:
            return ("FC", m.group(1))
        m = _UDT_RE.match(line)
        if m:
            return ("UDT", m.group(1))
    return ("unknown", "")


# ---------------------------------------------------------------------------
# DB file parser
# ---------------------------------------------------------------------------

# Instance DB: references an FB by unquoted or quoted name after NON_RETAIN
# e.g.:
#   NON_RETAIN
#   "Winch"
#
# Global DB with VAR or STRUCT:
#   NON_RETAIN
#   VAR ...
# or:
#   NON_RETAIN
#   STRUCT ...

_DB_NAME_RE = re.compile(r'^\s*DATA_BLOCK\s+"([^"]+)"', re.IGNORECASE)
_DB_FB_REF_RE = re.compile(r'^\s*"([^"]+)"\s*$')

_STRUCT_START_RE = re.compile(r"^\s*STRUCT\s*$", re.IGNORECASE)
_GLOBAL_VAR_START_RE = re.compile(r"^\s*VAR\b", re.IGNORECASE)


def _parse_db_file(
    lines: list[str],
) -> tuple[str, str | None, list[_VarInfo]]:
    """Parse a DATA_BLOCK file.

    Returns:
        (db_name, fb_ref_or_none, struct_members)

    - If ``fb_ref`` is not None, this is an instance DB of that FB.
    - If ``fb_ref`` is None, ``struct_members`` contains direct STRUCT or VAR members.
    """
    db_name = ""
    fb_ref: str | None = None
    members: list[_VarInfo] = []

    # First pass: find DB name
    for line in lines:
        m = _DB_NAME_RE.match(line)
        if m:
            db_name = m.group(1)
            break

    if not db_name:
        return ("", None, [])

    # Determine if instance DB or global DB
    # After NON_RETAIN (or directly after VERSION line), look for:
    #   - A quoted FB reference line → instance DB
    #   - STRUCT or VAR keyword → global DB with explicit members
    past_header = False
    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip lines until we're past the header (NON_RETAIN or VERSION)
        if stripped.upper().startswith("NON_RETAIN") or stripped.upper().startswith("VERSION"):
            past_header = True
            continue

        if not past_header:
            continue

        # Skip empty lines and attribute lines
        if not stripped or stripped.startswith("{"):
            continue

        # Check for FB reference (instance DB)
        m = _DB_FB_REF_RE.match(stripped)
        if m:
            fb_ref = m.group(1)
            return (db_name, fb_ref, [])

        # Check for STRUCT (global DB with struct members)
        if _STRUCT_START_RE.match(stripped):
            members = _parse_struct_members(lines[i + 1 :])
            return (db_name, None, members)

        # Check for VAR section (global DB with VAR members)
        if _GLOBAL_VAR_START_RE.match(stripped):
            members = _parse_var_sections(lines[i:])
            return (db_name, None, members)

        # BEGIN section means no more declarations
        if stripped.upper() == "BEGIN":
            break

    return (db_name, fb_ref, members)


def _parse_struct_members(lines: list[str]) -> list[_VarInfo]:
    """Parse member declarations from lines following a STRUCT keyword until END_STRUCT."""
    results: list[_VarInfo] = []
    struct_depth = 0
    struct_prefix: list[str] = []

    for line in lines:
        stripped = line.strip()

        if _STRUCT_CLOSE_RE.match(stripped):
            if struct_depth == 0:
                break
            struct_depth -= 1
            if struct_prefix:
                struct_prefix.pop()
            continue

        if _STRUCT_OPEN_RE.search(stripped):
            sm = re.match(
                r'^\s*(?P<name>"[^"]+"|[A-Za-z_]\w*)\s*(?:\{[^}]*\})?\s*:\s*Struct',
                stripped,
                re.IGNORECASE,
            )
            if sm:
                struct_var = _strip_quotes(sm.group("name"))
                prefix = ".".join(struct_prefix + [struct_var])
                cm = re.search(r"//\s*(.*?)\s*$", stripped)
                vcomment = cm.group(1) if cm else ""
                results.append(_VarInfo(prefix, "Struct", vcomment, "STRUCT"))
                struct_prefix.append(struct_var)
            struct_depth += 1
            continue

        m = _VAR_DECL_RE.match(stripped)
        if m:
            vname = _strip_quotes(m.group("name"))
            vtype = m.group("type").strip()
            vcomment = (m.group("comment") or "").strip()
            if struct_prefix:
                vname = ".".join(struct_prefix + [vname])
            results.append(_VarInfo(vname, vtype, vcomment, "STRUCT"))

    return results


# ---------------------------------------------------------------------------
# UDT parser
# ---------------------------------------------------------------------------

def _parse_udt(lines: list[str]) -> tuple[str, list[_VarInfo]]:
    """Parse a UDT file and return (udt_name, members)."""
    udt_name = ""
    for line in lines[:5]:
        m = _UDT_RE.match(line)
        if m:
            udt_name = m.group(1)
            break
    if not udt_name:
        return ("", [])

    # Find STRUCT..END_STRUCT
    for i, line in enumerate(lines):
        if _STRUCT_START_RE.match(line.strip()):
            members = _parse_struct_members(lines[i + 1 :])
            return (udt_name, members)

    return (udt_name, [])


# ---------------------------------------------------------------------------
# File reading helper
# ---------------------------------------------------------------------------

def _read_lines(path: Path) -> list[str]:
    """Read a file, handling BOM-encoded UTF-8."""
    return path.read_text(encoding="utf-8-sig").splitlines()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_catalog(
    project_dir: Path,
    project_name: str = "",
    plc_name: str = "",
) -> TagCatalog:
    """Build a tag catalog by parsing SCL files in the project src/ directory.

    Args:
        project_dir: Root of the project (e.g. ``projects/P2110``).
        project_name: Display name; inferred from directory if empty.
        plc_name: PLC device name; read from ``project.json`` if empty.

    Returns:
        A :class:`TagCatalog` with all discovered tags.
    """
    project_dir = Path(project_dir)
    src_dir = project_dir / "src"

    # Infer project name
    if not project_name:
        project_name = project_dir.name

    # Read plc_name from project.json if not provided
    if not plc_name:
        pj_path = project_dir / "project.json"
        if pj_path.exists():
            try:
                pj = json.loads(pj_path.read_text(encoding="utf-8-sig"))
                plc_name = pj.get("plc_name", "")
            except (json.JSONDecodeError, OSError):
                pass

    if not src_dir.is_dir():
        return TagCatalog(
            project=project_name,
            plc_name=plc_name,
            built_at=datetime.now(timezone.utc).isoformat(),
            tags=[],
        )

    # ---- Phase 1: parse all SCL files to collect FB/FC/UDT interfaces ----
    fb_interfaces: dict[str, list[_VarInfo]] = {}   # fb_name → vars
    fc_interfaces: dict[str, list[_VarInfo]] = {}
    udt_members: dict[str, list[_VarInfo]] = {}

    scl_files = list(src_dir.rglob("*.scl"))
    for scl_path in scl_files:
        try:
            lines = _read_lines(scl_path)
        except OSError:
            continue

        block_type, block_name = _detect_scl_block(lines)
        if block_type == "FB":
            fb_interfaces[block_name] = _parse_var_sections(lines)
        elif block_type == "FC":
            fc_interfaces[block_name] = _parse_var_sections(lines)
        elif block_type == "UDT":
            udt_name, members = _parse_udt(lines)
            if udt_name:
                udt_members[udt_name] = members

    # ---- Phase 2: parse DB files ----
    tags: list[TagEntry] = []
    seen_tags: set[str] = set()         # deduplicate across duplicate DB files
    seen_dbs: set[str] = set()          # skip duplicate DB names (e.g. same DB in src/ and src/Test/)
    db_files = list(src_dir.rglob("*.db"))

    for db_path in db_files:
        try:
            lines = _read_lines(db_path)
        except OSError:
            continue

        db_name, fb_ref, struct_members = _parse_db_file(lines)
        if not db_name or db_name in seen_dbs:
            continue
        seen_dbs.add(db_name)

        if fb_ref is not None:
            # Instance DB — look up FB interface and generate qualified tags
            fb_vars = fb_interfaces.get(fb_ref, [])
            for v in fb_vars:
                # Skip VAR_TEMP — not accessible externally
                if v.section == "VAR_TEMP":
                    continue
                qualified = f'"{db_name}".{v.name}'
                if qualified in seen_tags:
                    continue
                seen_tags.add(qualified)
                tags.append(TagEntry(
                    name=qualified,
                    data_type=v.data_type,
                    block=db_name,
                    description=v.comment,
                    category=_classify(db_name, v.name),
                ))
        else:
            # Global DB with explicit STRUCT or VAR members
            for v in struct_members:
                qualified = f'"{db_name}".{v.name}'
                if qualified in seen_tags:
                    continue
                seen_tags.add(qualified)
                tags.append(TagEntry(
                    name=qualified,
                    data_type=v.data_type,
                    block=db_name,
                    description=v.comment,
                    category=_classify(db_name, v.name),
                ))

    # ---- Phase 3: sort tags for stable output ----
    tags.sort(key=lambda t: t.name)

    return TagCatalog(
        project=project_name,
        plc_name=plc_name,
        built_at=datetime.now(timezone.utc).isoformat(),
        tags=tags,
    )


def save_catalog(catalog: TagCatalog, project_dir: Path) -> Path:
    """Save catalog to ``.tag_catalog.json`` and return the path."""
    project_dir = Path(project_dir)
    out_path = project_dir / ".tag_catalog.json"
    out_path.write_text(
        catalog.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return out_path


def load_catalog(project_dir: Path) -> TagCatalog | None:
    """Load a previously built catalog from ``.tag_catalog.json``, or None if not found."""
    project_dir = Path(project_dir)
    cat_path = project_dir / ".tag_catalog.json"
    if not cat_path.exists():
        return None
    try:
        data = json.loads(cat_path.read_text(encoding="utf-8"))
        return TagCatalog.model_validate(data)
    except (json.JSONDecodeError, OSError, ValueError):
        return None
