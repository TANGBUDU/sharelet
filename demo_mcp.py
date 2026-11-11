"""No-credential stdio MCP examples used by native and cross-client checks."""
from typing import Annotated, Literal
from pydantic import Field
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from public_tools import reading_time as estimate_reading, text_cleanup as cleanup


def reading_time(text: Annotated[str, Field(title="Text to read", min_length=1, max_length=500)],
                 speed: Annotated[int, Field(title="Reading units per minute", ge=100, le=1000)] = 300) -> dict:
    return estimate_reading(text, speed)


def text_cleanup(text: Annotated[str, Field(title="Your text", min_length=1, max_length=500)],
                 mode: Literal["keep", "upper", "lower"] = "keep",
                 preserve_lines: bool = True) -> dict:
    return cleanup(text, mode, preserve_lines)


mcp = FastMCP("Sharelet no-cost examples")
for function in (reading_time, text_cleanup):
    mcp.add_tool(function, description=function.__name__.replace("_", " ") + ". Runs locally without a model or credentials.",
                 annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))

if __name__ == "__main__":
    mcp.run(transport="stdio")
