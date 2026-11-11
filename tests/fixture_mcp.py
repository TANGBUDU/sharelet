"""Controlled MCP service for real transport/load checks; never calls an AI provider."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import time
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

mcp = FastMCP("Sharelet transport fixture", host="127.0.0.1")


def transform(text: Annotated[str, Field(min_length=1, max_length=500)],
              factor: Annotated[float, Field(ge=0, le=10)] = 1.0,
              enabled: bool = True,
              mode: Literal["keep", "upper", "lower"] = "keep",
              delay_ms: Annotated[int, Field(ge=0, le=5000)] = 0) -> dict:
    marker = os.environ.get("SHARELET_TEST_MARKER")
    if marker:
        with open(marker, "a", encoding="utf-8") as stream:
            stream.write("execution\n")
    if delay_ms:
        time.sleep(delay_ms / 1000)
    if text == "__failure__":
        raise ValueError("private fixture error must not reach a guest")
    output = text.upper() if mode == "upper" else text.lower() if mode == "lower" else text
    return {"output": output, "weighted_length": len(text)*factor, "enabled": enabled}


def nested(values: list[str]) -> dict:
    return {"values": values}


def destructive(target: str) -> dict:
    raise RuntimeError("This fixture must never be executed")


def optional_limit(text: str, limit: int | None = None) -> dict:
    return {'text':text,'limit':limit}


mcp.add_tool(transform, annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
mcp.add_tool(nested, annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
mcp.add_tool(destructive, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
mcp.add_tool(optional_limit, annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--http-metadata")
    args = parser.parse_args()
    if not args.http_metadata:
        mcp.run(transport="stdio")
        return
    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    application = mcp.streamable_http_app()
    expected = os.environ.get("SHARELET_TEST_TOKEN")
    if expected:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse
        class Authentication(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.headers.get("Authorization") != "Bearer " + expected:
                    return JSONResponse({"error": "unauthorised"}, status_code=401)
                return await call_next(request)
        application.add_middleware(Authentication)
    Path(args.http_metadata).write_text(json.dumps({"port": port}), encoding="utf-8")
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port, log_level="error"))
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
