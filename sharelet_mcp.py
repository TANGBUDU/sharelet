"""Portable owner MCP controller; guests never receive these management tools."""
import argparse
import asyncio
from typing import Annotated
from pydantic import Field
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from owner_client import OwnerClient


def create_controller(client, *, allow_public=True):
    server = FastMCP("Sharelet")

    def list_tools() -> dict:
        state = client.request("/api/agent/state")
        return {"tools": state["tools"], "connections": state.get("connections"),
                "image_provider_configured": state.get('image_provider_configured', False)}

    async def create_share(
        tool_id: str, title: Annotated[str, Field(min_length=1, max_length=80)],
        description: Annotated[str, Field(max_length=240)] = "",
        fixed_args: dict | None = None,
        style_notes: Annotated[str, Field(max_length=300)] = "",
        max_calls: Annotated[int, Field(ge=1, le=100)] = 5,
        expires_seconds: Annotated[int, Field(ge=60, le=86400)] = 3600,
        public: bool = False,
    ) -> dict:
        """Request an immutable share draft for the owner to review in the private workspace.
        This cannot open a public address, approve a request, or create a usable link.
        Both local previews and public shares require the owner's separate approval.
        """
        if public and not allow_public:
            return {"ok": False, "error": "This controller only permits local previews."}
        result = await asyncio.to_thread(client.request, "/api/agent/drafts",
                                         {"tool_id": tool_id, "title": title, "description": description,
                                          "fixed_args": fixed_args or {}, "max_calls": max_calls,
                                          "style_notes": style_notes,
                                          "expires_seconds": expires_seconds,
                                          "public": public})
        result["audience"] = "awaiting owner approval"
        return result

    def list_shares() -> dict:
        state = client.request("/api/agent/state")
        return {"shares": [{key: share.get(key) for key in
                            ("id", "title", "status", "remaining", "max_calls", "url", "public_enabled")}
                           for share in state["shares"]], 'drafts': state.get('drafts', [])}

    def revoke_share(share_id: Annotated[str, Field(pattern=r"^[a-f0-9]{24}$")]) -> dict:
        """Close a previously published share. Already downloaded output cannot be recalled."""
        return client.request("/api/agent/shares/" + share_id + "/revoke", {})

    def connection_status() -> dict:
        state = client.request("/api/agent/state")
        return {"connection": state["connection"], "connections": state.get("connections"),
                "usage": state.get("usage"), "image_allowance": state.get("budget")}

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    server.add_tool(list_tools, name="sharelet_list_tools", description="List tools available to share and their constrained input schemas. Does not run the tools.", annotations=read)
    server.add_tool(create_share, name="sharelet_create_share", annotations=write)
    server.add_tool(list_shares, name="sharelet_list_shares", description="List this owner's shares and remaining allowances.", annotations=read)
    server.add_tool(revoke_share, name="sharelet_revoke_share", annotations=write)
    server.add_tool(connection_status, name="sharelet_connection_status", description="Read the local Sharelet service and connection status. No credentials are returned.", annotations=read)
    return server


def main():
    parser = argparse.ArgumentParser(description="Sharelet MCP controller for compatible agents")
    parser.add_argument("--data-dir")
    parser.add_argument("--no-auto-start", action="store_true")
    parser.add_argument("--local-only", action="store_true", help="Prevent this agent connection from publishing public links")
    args = parser.parse_args()
    client = OwnerClient(args.data_dir, auto_start=not args.no_auto_start, role='agent')
    create_controller(client, allow_public=not args.local_only).run(transport="stdio")


if __name__ == "__main__":
    main()
