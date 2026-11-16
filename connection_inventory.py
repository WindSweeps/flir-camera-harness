from typing import Annotated, Literal, Self

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeviceConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    resource: Annotated[str, Field(min_length=1, max_length=512, pattern=r"^\S+$")]
    label: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class ConnectionInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    devices: Annotated[list[DeviceConnection], Field(max_length=256)]

    @model_validator(mode="after")
    def unique_resources(self) -> Self:
        if len({device.resource for device in self.devices}) != len(self.devices):
            raise ValueError("Device resources must be unique")
        return self


def create_server() -> FastMCP:
    server = FastMCP("flir-camera-inventory")
    current = ConnectionInventory(schema_version=1, devices=[])

    @server.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False,
    ))
    def set_connection_inventory(inventory: ConnectionInventory) -> dict[str, int]:
        """Replace the hub-reported connection list. An empty list clears it."""
        nonlocal current
        current = inventory
        return {"schema_version": current.schema_version, "device_count": len(current.devices)}

    @server.tool(annotations=ToolAnnotations(
        readOnlyHint=True, idempotentHint=True, openWorldHint=False,
    ))
    def get_connection_inventory() -> ConnectionInventory:
        """Read the latest hub report. Camera discovery and state come from flir-camera."""
        return current

    return server


if __name__ == "__main__":
    create_server().run(transport="stdio")
