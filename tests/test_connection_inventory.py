import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check_round_trip():
    script = Path(__file__).resolve().parents[1] / "connection_inventory.py"
    service = StdioServerParameters(command=sys.executable, args=[str(script)])
    async with stdio_client(service) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "get_connection_inventory", "set_connection_inventory",
        }
        empty = {"schema_version": 1, "devices": []}
        initial = await client.call_tool("get_connection_inventory")
        assert initial.structuredContent == empty
        report = {
            "schema_version": 1,
            "devices": [{"resource": "flir:serial:12345678", "label": "FLIR 相机"}],
        }
        accepted = await client.call_tool("set_connection_inventory", {"inventory": report})
        assert accepted.structuredContent == {"schema_version": 1, "device_count": 1}
        for invalid in (
            {"schema_version": 2, "devices": []},
            {"schema_version": 1, "devices": report["devices"] * 2},
            {"schema_version": 1, "devices": [{"resource": "has whitespace"}]},
        ):
            rejected = await client.call_tool("set_connection_inventory", {"inventory": invalid})
            assert rejected.isError
        latest = await client.call_tool("get_connection_inventory")
        assert latest.structuredContent == report
        cleared = await client.call_tool("set_connection_inventory", {"inventory": empty})
        assert cleared.structuredContent == {"schema_version": 1, "device_count": 0}
        latest = await client.call_tool("get_connection_inventory")
        assert latest.structuredContent == empty


def test_stdio_round_trip():
    asyncio.run(check_round_trip())
