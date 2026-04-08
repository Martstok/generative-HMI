"""OPC UA client for live tag reads and address-space browsing (asyncua)."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from asyncua import Client, Node, ua
from loguru import logger

SIEMENS_NS_URI = "http://www.siemens.com/simatic-s7-opcua"

# ---------------------------------------------------------------------------
# Type mapping: ua.VariantType int value -> PLC type string
# ---------------------------------------------------------------------------
_TYPE_MAP: dict[int, str] = {
    ua.VariantType.Boolean.value: "Bool",
    ua.VariantType.SByte.value: "SInt",
    ua.VariantType.Byte.value: "USInt",
    ua.VariantType.Int16.value: "Int",
    ua.VariantType.UInt16.value: "UInt",
    ua.VariantType.Int32.value: "DInt",
    ua.VariantType.UInt32.value: "UDInt",
    ua.VariantType.Int64.value: "LInt",
    ua.VariantType.UInt64.value: "ULInt",
    ua.VariantType.Float.value: "Real",
    ua.VariantType.Double.value: "LReal",
    ua.VariantType.String.value: "String",
    ua.VariantType.DateTime.value: "DTL",
}


def _plc_type(variant_type: int) -> str:
    """Map a VariantType int to a PLC type string."""
    return _TYPE_MAP.get(variant_type, f"Unknown({variant_type})")


# ---------------------------------------------------------------------------
# Write-value casting
# ---------------------------------------------------------------------------

def _cast_for_write(variant_type: ua.VariantType, value: Any) -> Any:
    """Cast a Python value to the correct type for OPC UA write."""
    s = str(value).strip()
    if variant_type == ua.VariantType.Boolean:
        return s.lower() in ("true", "1", "1.0")
    if variant_type in (ua.VariantType.Float, ua.VariantType.Double):
        return float(s)
    if variant_type in (
        ua.VariantType.Int16, ua.VariantType.Int32, ua.VariantType.Int64,
        ua.VariantType.UInt16, ua.VariantType.UInt32, ua.VariantType.UInt64,
        ua.VariantType.SByte, ua.VariantType.Byte,
    ):
        return int(float(s))
    return s


# ---------------------------------------------------------------------------
# Node-ID helper
# ---------------------------------------------------------------------------

def _catalog_to_node_id(tag_name: str, ns: int) -> str:
    """Convert a catalog tag name to an OPC UA node ID.

    Input:  ``"HMI_DB".System.xRemoteCtrl_Sts``
    Output: ``ns=3;s="HMI_DB"."System"."xRemoteCtrl_Sts"``
    """
    tokens = re.findall(r'"[^"]*"|[^.]+', tag_name)
    parts = []
    for tok in tokens:
        if tok.startswith('"'):
            parts.append(tok)
        else:
            parts.append(f'"{tok}"')
    return f'ns={ns};s={".".join(parts)}'


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class OpcuaTagClient:
    """Async OPC UA client wrapping *asyncua* for tag reads and browsing."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._client = Client(endpoint)
        self._lock = asyncio.Lock()
        self._connected = False
        self._ns: int = 0
        self._node_cache: dict[str, Node] = {}

    # -- connection lifecycle ------------------------------------------------

    async def connect(self) -> None:
        async with self._lock:
            if self._connected:
                return
            logger.info("OPC UA connecting to {}", self.endpoint)
            await self._client.connect()
            try:
                self._ns = await self._client.get_namespace_index(SIEMENS_NS_URI)
            except ValueError:
                logger.warning("Siemens namespace not found, using ns=2")
                self._ns = 2
            self._connected = True
            logger.info("OPC UA connected (ns={})", self._ns)

    async def disconnect(self) -> None:
        async with self._lock:
            if not self._connected:
                return
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._node_cache.clear()
            self._connected = False
            logger.info("OPC UA disconnected from {}", self.endpoint)

    async def _ensure_connected(self) -> None:
        if not self._connected:
            await self.connect()

    # -- node resolution -----------------------------------------------------

    def _resolve_node(self, tag_name: str) -> Node:
        """Get or create a *Node* object for a catalog tag name."""
        if tag_name not in self._node_cache:
            node_id = _catalog_to_node_id(tag_name, self._ns)
            self._node_cache[tag_name] = self._client.get_node(node_id)
        return self._node_cache[tag_name]

    # -- read ----------------------------------------------------------------

    async def read_tags(self, tag_names: list[str]) -> dict[str, Any]:
        """Batch-read tag values.  Returns ``{tag_name: value}``, *None* on failure."""
        await self._ensure_connected()
        result: dict[str, Any] = {t: None for t in tag_names}

        try:
            nodes = [self._resolve_node(t) for t in tag_names]
            values = await self._client.read_values(nodes)
            for tag, val in zip(tag_names, values):
                result[tag] = val
        except Exception as exc:
            logger.warning("OPC UA read failed, reconnecting: {}", exc)
            # Reconnect and retry once
            self._connected = False
            self._node_cache.clear()
            try:
                await self._ensure_connected()
                nodes = [self._resolve_node(t) for t in tag_names]
                values = await self._client.read_values(nodes)
                for tag, val in zip(tag_names, values):
                    result[tag] = val
            except Exception as exc2:
                logger.error("OPC UA read retry failed: {}", exc2)

        return result

    # -- write ---------------------------------------------------------------

    async def write_tag(self, tag_name: str, value: Any) -> bool:
        """Write a value to a single OPC UA tag.  Returns *True* on success."""
        await self._ensure_connected()
        try:
            node = self._resolve_node(tag_name)
            # Read the current data type so we can cast correctly
            dv = await node.read_data_type_as_variant_type()
            cast = _cast_for_write(dv, value)
            await node.write_value(cast)
            logger.info("OPC UA wrote {}={}", tag_name, cast)
            return True
        except Exception as exc:
            logger.error("OPC UA write failed for {}: {}", tag_name, exc)
            return False

    # -- browse --------------------------------------------------------------

    async def browse_tags(self, filter_block: str = "") -> list[dict]:
        """Browse the OPC UA address space and return tag entries."""
        await self._ensure_connected()
        results: list[dict] = []

        try:
            objects = self._client.nodes.objects
            db_nodes = await self._find_db_nodes(objects)

            for db_node in db_nodes:
                db_name = (await db_node.read_browse_name()).Name
                quoted_db = f'"{db_name}"' if not db_name.startswith('"') else db_name

                if filter_block and db_name.strip('"') != filter_block.strip('"'):
                    continue

                await self._browse_recursive(
                    db_node, [quoted_db], db_name.strip('"'), results
                )
        except Exception as exc:
            logger.error("OPC UA browse failed: {}", exc)

        return results

    async def _find_db_nodes(self, objects: Node) -> list[Node]:
        """Navigate to *DataBlocksGlobal* or find DB-level nodes."""
        candidates: list[Node] = []

        try:
            top_children = await objects.get_children()

            # Level 1
            for child in top_children:
                bn = await child.read_browse_name()
                if bn.Name == "DataBlocksGlobal":
                    return await child.get_children()
                if bn.NamespaceIndex == self._ns:
                    candidates.append(child)

            # Level 2
            for candidate in candidates:
                try:
                    sub_children = await candidate.get_children()
                    for sub in sub_children:
                        bn = await sub.read_browse_name()
                        if bn.Name == "DataBlocksGlobal":
                            return await sub.get_children()
                except Exception:
                    continue

            # Level 3
            for child in top_children:
                try:
                    sub_children = await child.get_children()
                    for sub in sub_children:
                        bn = await sub.read_browse_name()
                        if bn.Name == "DataBlocksGlobal":
                            return await sub.get_children()
                        try:
                            sub_sub_children = await sub.get_children()
                            for subsub in sub_sub_children:
                                bn2 = await subsub.read_browse_name()
                                if bn2.Name == "DataBlocksGlobal":
                                    return await subsub.get_children()
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("Failed to navigate address space: {}", exc)

        # Fallback: return candidates in Siemens namespace
        return candidates

    async def _browse_recursive(
        self,
        node: Node,
        path: list[str],
        block_name: str,
        results: list[dict],
    ) -> None:
        """Recursively descend, collecting leaf Variable nodes."""
        try:
            children = await node.get_children()
        except Exception:
            return

        if not children:
            # Leaf node -- check if it is a variable
            try:
                node_class = await node.read_node_class()
                if node_class == ua.NodeClass.Variable:
                    try:
                        dv = await node.read_data_type_as_variant_type()
                        dtype = _plc_type(dv.value)
                    except Exception:
                        dtype = "Unknown"

                    qualified_name = ".".join(path)
                    results.append(
                        {
                            "name": qualified_name,
                            "data_type": dtype,
                            "block": block_name,
                            "description": "",
                        }
                    )
            except Exception:
                pass
            return

        for child in children:
            try:
                child_class = await child.read_node_class()
                bn = await child.read_browse_name()
                segment = bn.Name

                if child_class == ua.NodeClass.Variable:
                    sub_children = await child.get_children()
                    if not sub_children:
                        # True leaf variable
                        try:
                            dv = await child.read_data_type_as_variant_type()
                            dtype = _plc_type(dv.value)
                        except Exception:
                            dtype = "Unknown"

                        qualified_name = ".".join(path + [segment])
                        results.append(
                            {
                                "name": qualified_name,
                                "data_type": dtype,
                                "block": block_name,
                                "description": "",
                            }
                        )
                    else:
                        # Struct variable -- descend further
                        await self._browse_recursive(
                            child, path + [segment], block_name, results
                        )

                elif child_class == ua.NodeClass.Object:
                    # Folder / struct -- descend
                    await self._browse_recursive(
                        child, path + [segment], block_name, results
                    )
            except Exception:
                continue


# ---------------------------------------------------------------------------
# Module-level client pool
# ---------------------------------------------------------------------------

_clients: dict[str, OpcuaTagClient] = {}


def get_opcua_client(endpoint: str) -> OpcuaTagClient:
    """Return a shared *OpcuaTagClient* for the given endpoint."""
    if endpoint not in _clients:
        _clients[endpoint] = OpcuaTagClient(endpoint)
    return _clients[endpoint]


async def disconnect_all() -> None:
    """Disconnect all cached OPC UA clients (called on shutdown)."""
    for c in list(_clients.values()):
        try:
            await c.disconnect()
        except Exception:
            pass
    _clients.clear()
