"""A narrow MCP adapter for the owner's installed Permission Canvas plugin."""
from typing import Annotated,Literal
from pydantic import Field
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from sandbox import load_library,run_demo

mcp=FastMCP("Permission Canvas sandbox")


def try_permission_canvas(
    scenario:Literal["Edit an allowed note","Change read-only revenue","Send an invitation",
                     "Reveal a hidden credential","Copy to clipboard","Observe the agent view"]="Edit an allowed note",
    text:Annotated[str,Field(title="New note",description="Used only for editing the fictional note. No real desktop is connected.",max_length=100)]="Hello, Sharelet!",
) -> dict:
    """Try six operations through the real Permission Canvas permission/redaction backend.
    Uses only a fresh synthetic in-memory workspace. No real desktop, clipboard,
    invitation, payment, model or paid API is connected. Yellow stays unapproved.
    """
    return run_demo(scenario,text)


mcp.add_tool(try_permission_canvas,
    description="Try the installed Permission Canvas backend on a fictional workspace. No real desktop or paid API is connected.",
    annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))

if __name__=="__main__":
    load_library()  # Refuse to start unless the real installed plugin is loadable.
    mcp.run(transport="stdio")
