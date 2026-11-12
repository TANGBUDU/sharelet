"""Hermes entry point: public PluginContext APIs, one profile-owned service."""
import json
from pathlib import Path

from .host import HermesBridge
from .relay import DEFAULT_RELAY
from .service import Runtime

_RUNTIME = {}


def register(ctx):
    info = {"service": "disabled", "version": "0.5.2"}
    def status(args, **kwargs):
        runtime = _RUNTIME.get("runtime")
        return json.dumps({"ok": True, **(runtime.info if runtime else info)})
    ctx.register_tool(name="sharelet_status",toolset="sharelet",schema={
        "name":"sharelet_status","description":"Show the Sharelet service and creator entry location. Does not publish links or disclose the creator credential.",
        "parameters":{"type":"object","properties":{},"additionalProperties":False}},handler=status)
    if ctx.get_config("enabled",False) is not True:
        return
    # Snapshot the SDK's profile-owned data directory while register() is scoped.
    root = Path(ctx.state.data_dir).resolve()
    bridge = HermesBridge(ctx)
    port, guest_port = ctx.get_config("port",9271), ctx.get_config("guest_port",9272)
    if any(type(value) is not int or not 1024<=value<=65535 for value in (port,guest_port)) or port==guest_port:
        raise ValueError("Choose two distinct preferred ports between 1024 and 65535")
    limit=ctx.get_config("generation_limit",30)
    if type(limit) is not int or not 0<=limit<=1000:
        raise ValueError("generation_limit must be between 0 and 1000")
    executions=ctx.get_config("execution_limit",10000)
    if type(executions) is not int or not 1<=executions<=100000:
        raise ValueError("execution_limit must be between 1 and 100000")
    runtime=Runtime(root,bridge.call,bridge.catalog,connection={"mode":"hermes","label":"Connected through Hermes"},
                    port=port,guest_port=guest_port,binary=ctx.get_config("cloudflared_path",None),generation_limit=limit,
                    execution_limit=executions,connector=bridge,
                    # Raw tunnel links by default. A creator may explicitly configure their own
                    # relay or a fixed public_origin (Tailscale Funnel, named tunnel).
                    relay_url=ctx.get_config("relay_url",DEFAULT_RELAY),public_origin=ctx.get_config("public_origin",""))
    ctx.on_unload(runtime.close)
    runtime.start()
    _RUNTIME["runtime"]=runtime
