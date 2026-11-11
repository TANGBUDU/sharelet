"""Public Hermes APIs: scoped discovery, explicit server grants and native dispatch."""
from __future__ import annotations

import contextvars
import json
import hashlib
from pathlib import Path
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError

try:
    from .catalog import CONTROL_TOOLS, UTILITY_TOOLS, tool_binding
    from .presets import image_bindings
    from .sharelet import CapsuleError, current_dispatch_guard, normalise_schema
except ImportError:
    from catalog import CONTROL_TOOLS, UTILITY_TOOLS, tool_binding
    from presets import image_bindings
    from sharelet import CapsuleError, current_dispatch_guard, normalise_schema


class HermesBridge:
    def __init__(self, ctx, *, inventory=None, configuration=None, call_timeout=180):
        self.ctx = ctx
        self.context = contextvars.copy_context()
        self._inventory_reader = inventory
        self._configuration_reader = configuration
        if not 0<call_timeout<=180:
            raise ValueError('Tool calls need a deadline of at most 180 seconds')
        self.call_timeout=call_timeout
        self._rpc_slots=threading.BoundedSemaphore(4)

    def _run(self, callback):
        return self.context.copy().run(callback)

    def _configuration(self):
        if self._configuration_reader:
            return self._configuration_reader()
        # These modules and non-private methods are in Hermes' published plugin API.
        from hermes_cli.config import load_config
        return load_config() or {}

    def _inventory(self):
        if self._inventory_reader:
            return self._inventory_reader()
        from tools.registry import registry
        try:
            from .native_tools import configured_grants
        except ImportError:
            from native_tools import configured_grants
        native_names={grant.get('tool') for grant in configured_grants(self._configuration(),self.ctx.plugin_id)}
        rows=[]
        for name in registry.get_all_tool_names():
            row={'name':name,'toolset':registry.get_toolset_for_tool(name),'schema':registry.get_schema(name)}
            if name in native_names:
                import inspect
                entry=registry.get_entry(name)
                if entry:
                    row.update(handler_source=inspect.getsourcefile(inspect.unwrap(entry.handler)),handler_module=entry.handler.__module__)
            rows.append(row)
        return rows

    def _grants(self):
        config = self._configuration()
        plugin_id = self.ctx.plugin_id
        entry = config.get("plugins", {}).get("entries", {}).get(plugin_id, {})
        granted = entry.get("mcp_allowlist", [])
        servers = config.get("mcp_servers", {})
        return {name for name in granted if isinstance(name, str) and name in servers}

    def call(self, server, tool, args):
        try:
            from .native_tools import PREFIX,native_catalog
        except ImportError:
            from native_tools import PREFIX,native_catalog
        native=server.startswith(PREFIX) and not tool.startswith('mcp__')
        guard = current_dispatch_guard()
        def invoke():
            if guard:
                guard()
            if native:
                choices=native_catalog(self.ctx,self._configuration(),self._inventory())
                if not any(row['server']==server and row['tool']==tool and row['supported'] for row in choices):
                    raise PermissionError('This native plugin tool is no longer authorised or available')
            elif not tool.startswith("mcp__"):
                # Curated recipes preserve the public MCP facade's own grant checks.
                return self.ctx.call_mcp(server, tool, args, timeout=180)
            if not native and server not in self._grants():
                raise PermissionError("This MCP server is no longer authorised for Sharelet")
            current = next((item for item in self._inventory()
                            if item["name"] == tool and (native or item["toolset"] == "mcp-" + server)), None)
            if current is None:
                raise RuntimeError("The selected tool is no longer available")
            # Dispatch the exact registered name. Do not guess raw names from Hermes'
            # sanitised or hash-shortened provider names.
            if guard:
                guard()
            raw = self.ctx.dispatch_tool(tool, args)
            value = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(value, dict) and (value.get('isError') is True or value.get('ok') is False
                    or set(value)=={'error'} and isinstance(value['error'],str)):
                return {"ok": False}
            if isinstance(value, dict) and "result" in value:
                return {"ok": True, "result": value["result"],
                        **({"structuredContent": value["structuredContent"]}
                           if "structuredContent" in value else {})}
            return {"ok": True, "result": value}
        if not native and not tool.startswith('mcp__'):
            return self._run(invoke)
        # Earlier calls that timed out may still hold slots. Wait within the same deadline;
        # if no slot frees up, the tool was never contacted, so the attempt is not charged.
        started=time.monotonic()
        if not self._rpc_slots.acquire(timeout=self.call_timeout):
            return {'ok':False,'not_sent':True}
        remaining=self.call_timeout-(time.monotonic()-started)
        if remaining<min(1.0,self.call_timeout/2):
            # Too little time is left to wait for a result. Do not start a call whose
            # outcome would be reported as uncertain before it could finish.
            self._rpc_slots.release()
            return {'ok':False,'not_sent':True}
        future=Future()
        def execute():
            try:future.set_result(self._run(invoke))
            except BaseException as error:future.set_exception(error)
            finally:self._rpc_slots.release()
        threading.Thread(target=execute,daemon=True,name='sharelet-native-rpc').start()
        try:return future.result(timeout=remaining)
        except TimeoutError:return {'ok':False,'uncertain':True}

    def catalog(self):
        def read():
            grants = self._grants()
            entries = []
            image_server = self.ctx.get_config("image_server", "sharelet_images")
            image_tool = self.ctx.get_config("image_tool", "generate_avatar")
            # Operators can supply exact curated bindings for an image adapter.
            image_present = False
            import re
            image_name = "mcp__" + re.sub(r"[^A-Za-z0-9_]", "_", image_server) + "__" + re.sub(r"[^A-Za-z0-9_]", "_", image_tool)
            config = self._configuration()
            manager_servers = {name for name, spec in config.get("mcp_servers", {}).items()
                               if isinstance(spec, dict) and any(str(arg).replace('\\','/').rsplit('/',1)[-1] in ('sharelet_mcp.py','sharelet_mcp','sharelet.sharelet_mcp')
                                                                 for arg in spec.get('args', []))}
            inventory=self._inventory()
            for item in inventory:
                schema = item.get("schema")
                if not isinstance(schema, dict):
                    continue
                server = next((name for name in grants if item.get("toolset") == "mcp-" + name), None)
                if server is None or server in manager_servers or not str(item.get("name", "")).startswith("mcp__"):
                    continue
                name = item["name"]
                if server == image_server and name == image_name:
                    hints = item.get('annotations',schema.get('annotations')) or {}
                    if hints.get('destructiveHint') is True:
                        entries.append(tool_binding(server,name,schema.get('parameters',{}),annotations=hints))
                    else:
                        image_present = True
                    continue
                # Exclude host-generated resource/event utilities and management tools.
                suffix = name.rsplit("__", 1)[-1]
                if suffix in UTILITY_TOOLS or suffix in CONTROL_TOOLS or (server == image_server and suffix == "recipe_info"):
                    continue
                hints = item.get("annotations", schema.get("annotations"))
                # Hermes currently drops upstream annotations. Only our exact bundled
                # no-model adapter is known read-only without them; everything else
                # needs an explicit, tool-specific owner decision.
                spec = config.get("mcp_servers", {}).get(server, {})
                try:
                    bundled = (Path(spec.get("command", "")).resolve() == Path(sys.executable).resolve()
                               and isinstance(spec.get('args'),list) and len(spec['args'])==1
                               and Path(spec['args'][0]).resolve() == (Path(__file__).parent/'demo_mcp.py').resolve()
                               and suffix in ("text_cleanup", "reading_time"))
                except (OSError, TypeError, ValueError):
                    bundled = False
                if hints is None and bundled:
                    hints = {"readOnlyHint": True, "destructiveHint": False}
                entry = tool_binding(server, name, schema.get("parameters", {}),
                                            title=schema.get("title") or suffix,
                                            description=schema.get("description", ""), annotations=hints)
                entry["source_revision"] = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
                entries.append(entry)
            try:
                from .native_tools import native_catalog
            except ImportError:
                from native_tools import native_catalog
            entries.extend(native_catalog(self.ctx,config,inventory))
            if image_present:
                spec = config.get('mcp_servers',{}).get(image_server,{})
                try:
                    bundled = (Path(spec.get('command','')).resolve()==Path(sys.executable).resolve()
                               and isinstance(spec.get('args'),list) and len(spec['args'])==1
                               and Path(spec['args'][0]).resolve()==(Path(__file__).parent/'demo_image_mcp.py').resolve())
                except (OSError,TypeError,ValueError):
                    bundled = False
                recipes = image_bindings(image_server,image_tool)
                for recipe in recipes:
                    recipe['source_revision'] = hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()
                    if not bundled:
                        recipe.update(safety='review_required',requires_side_effects_approval=True)
                entries.extend(recipes)
            for entry in entries:
                if entry.get("supported"):
                    entry["schema"] = normalise_schema(entry["schema"])
            return sorted(entries, key=lambda item: (item.get("kind") == "image", item["title"]))
        return self._run(read)

    def connection_status(self):
        def read():
            grants = self._grants()
            try:
                from .native_tools import native_catalog
            except ImportError:
                from native_tools import native_catalog
            native=native_catalog(self.ctx,self._configuration(),self._inventory())
            return {"mode": "hermes", "import_supported": False,
                    "servers": [{"name": name, "status": "authorised"} for name in sorted(grants)],
                    "native_tools":[row['provenance'] for row in native if row.get('supported')],
                    "message": "Tools come from explicit MCP or native-plugin grants in the current Hermes profile."}
        return self._run(read)
