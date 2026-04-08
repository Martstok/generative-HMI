"""Mock OPC UA servers for an offshore wind farm SCADA control center.

Hosts multiple independent servers (one per PLC: WTGxx turbines, SUB01
substation, MET01 met mast, SCADA plant master) in a single process, each on
its own port. A shared simulation engine drives physically-coherent values
across all servers.

Usage::

    python -m tools.gui.mock_opcua_server [--turbines N]
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Any

from asyncua import Server, ua
from loguru import logger

from tools.gui.mock_sim import run_simulation
from tools.gui.mock_tag_defs import (
    MET_TAGS,
    SCADA_TAGS,
    SUB_TAGS,
    WTG_TAGS,
)

SIEMENS_NS_URI = "http://www.siemens.com/simatic-s7-opcua"

# (plc_name, port, tag_defs)
PLC_DEFS: list[tuple[str, int, dict[str, list[tuple[str, ua.VariantType, Any]]]]] = [
    ("WTG01", 4841, WTG_TAGS),
    ("WTG02", 4842, WTG_TAGS),
    ("WTG03", 4843, WTG_TAGS),
    ("WTG04", 4844, WTG_TAGS),
    ("SUB01", 4845, SUB_TAGS),
    ("MET01", 4846, MET_TAGS),
    ("SCADA", 4847, SCADA_TAGS),
]


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------

async def create_plc_server(
    name: str,
    port: int,
    tag_defs: dict[str, list[tuple[str, ua.VariantType, Any]]],
) -> tuple[Server, dict[str, Any]]:
    """Build one mock OPC UA server and return (server, sim_vars)."""
    server = Server()
    await server.init()
    endpoint = f"opc.tcp://0.0.0.0:{port}"
    server.set_endpoint(endpoint)
    server.set_server_name(f"Mock {name} OPC UA Server")

    ns = await server.register_namespace(SIEMENS_NS_URI)
    objects = server.nodes.objects

    dbg = await objects.add_folder(ns, "DataBlocksGlobal")
    hmi_db = await dbg.add_folder(ns, "HMI_DB")

    sim_vars: dict[str, Any] = {}

    for struct_name, members in tag_defs.items():
        folder_nid = ua.NodeId(f'"HMI_DB"."{struct_name}"', ns)
        folder = await hmi_db.add_folder(folder_nid, ua.QualifiedName(struct_name, ns))
        for var_name, vtype, initial in members:
            var_nid = ua.NodeId(f'"HMI_DB"."{struct_name}"."{var_name}"', ns)
            var = await folder.add_variable(
                var_nid, ua.QualifiedName(var_name, ns), initial, varianttype=vtype,
            )
            await var.set_writable()
            sim_vars[f"{struct_name}.{var_name}"] = var

    total = sum(len(m) for m in tag_defs.values())
    logger.info("[{}] address space ready: {} structs, {} tags", name, len(tag_defs), total)
    return server, sim_vars


# ---------------------------------------------------------------------------
# Multi-server runner
# ---------------------------------------------------------------------------

async def run_all(turbines: int = 4) -> None:
    """Start every PLC server and the shared simulation loop."""
    turbines = max(0, min(4, turbines))

    # Filter PLC_DEFS: include first N WTGs plus the fixed infrastructure PLCs
    defs: list[tuple[str, int, dict]] = []
    wtg_count = 0
    for name, port, tags in PLC_DEFS:
        if name.startswith("WTG"):
            if wtg_count < turbines:
                defs.append((name, port, tags))
                wtg_count += 1
        else:
            defs.append((name, port, tags))

    servers: list[tuple[str, Server, str]] = []
    sim_vars_per_plc: dict[str, dict[str, Any]] = {}

    for name, port, tags in defs:
        server, sim_vars = await create_plc_server(name, port, tags)
        servers.append((name, server, f"opc.tcp://0.0.0.0:{port}"))
        sim_vars_per_plc[name] = sim_vars

    async with contextlib.AsyncExitStack() as stack:
        for name, server, endpoint in servers:
            await stack.enter_async_context(server)
            logger.info("Mock {} OPC UA server listening on {}", name, endpoint)

        world_state: dict[str, Any] = {}
        sim_task = asyncio.create_task(run_simulation(world_state, sim_vars_per_plc))
        try:
            await asyncio.Future()  # run forever
        finally:
            sim_task.cancel()
            with contextlib.suppress(Exception):
                await sim_task


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main() -> None:
    turbines = 4
    args = sys.argv[1:]
    if "--turbines" in args:
        idx = args.index("--turbines")
        if idx + 1 < len(args):
            turbines = int(args[idx + 1])

    try:
        asyncio.run(run_all(turbines))
    except KeyboardInterrupt:
        logger.info("Mock servers stopped")


if __name__ == "__main__":
    main()
