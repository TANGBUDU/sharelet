"""Owner-configured MCP connections. Source credentials remain in their existing files."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

try:
    from .catalog import CONTROL_TOOLS, UTILITY_TOOLS, tool_binding
    from .image_provider import atomic_private, capture_profile_key_reader
    from .presets import image_bindings
    from .public_tools import basic_bindings, direct_dispatch
    from .sharelet import CapsuleError, DispatchNotSent, current_dispatch_guard
except ImportError:
    from catalog import CONTROL_TOOLS, UTILITY_TOOLS, tool_binding
    from image_provider import atomic_private, capture_profile_key_reader
    from presets import image_bindings
    from public_tools import basic_bindings, direct_dispatch
    from sharelet import CapsuleError, DispatchNotSent, current_dispatch_guard


def _server_table(path):
    path = Path(path).expanduser()
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise CapsuleError("Choose an existing, absolute MCP configuration file path.")
    text = path.read_text(encoding="utf-8-sig")
    try:
        if path.suffix.lower() == ".toml":
            import tomllib
            data = tomllib.loads(text)
        elif path.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError:
                raise CapsuleError("Install PyYAML to import a YAML profile, or use a JSON/TOML MCP configuration.") from None
            data = yaml.safe_load(text)
        else:
            def unique(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result
            data = json.loads(text, object_pairs_hook=unique)
        if not isinstance(data, dict):
            raise ValueError("configuration")
        table = data.get("mcpServers", data.get("mcp_servers", data.get("servers", {})))
        if not isinstance(table, dict):
            raise ValueError("servers")
        return {name: spec for name, spec in table.items()
                if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
                and isinstance(spec, dict) and spec.get("enabled", True) is not False}
    except CapsuleError:
        raise
    except (ValueError, TypeError):
        raise CapsuleError("This file is not a supported JSON, TOML or YAML MCP configuration.") from None


def _expand(value):
    if not isinstance(value, str):
        raise ValueError("Expected text configuration")
    def replace(match):
        name = match.group(1)
        if name not in os.environ:
            raise ValueError("A required environment variable is missing")
        return os.environ[name]
    return re.sub(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


def _transport(spec):
    return "stdio" if spec.get("command") else "http" if isinstance(spec.get("url"), str) and spec.get("type", "http") in ("http", "streamable-http", "streamable_http") else "unsupported"


class PortableBridge:
    def __init__(self, root, *, builtin=True, images=True):
        self.root = Path(root)
        self.path = self.root / "connections.json"
        self.builtin, self.images = builtin, images
        self._secret_reader = capture_profile_key_reader()
        self._lock = threading.RLock()
        self._loop = self._thread = None
        self._connections = {}
        self._signature = None
        self._closed = False

    def _settings(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}

    def _stamp(self):
        paths = [self.path]
        try:
            settings = self._settings()
            if isinstance(settings.get("import"), dict):
                paths.append(Path(settings["import"].get("path", "")))
            return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) if p.is_file() else (str(p), None, None)
                         for p in paths)
        except (OSError, ValueError, TypeError):
            return ("invalid",)

    def _ensure_current(self):
        if self._stamp() != self._signature:
            self.refresh()

    def inspect_config(self, body):
        if not isinstance(body, dict) or set(body) != {"path"}:
            raise CapsuleError("Supply the path to an existing MCP configuration.")
        table = _server_table(body["path"])
        return {"servers": [{"name": name, "transport": _transport(spec),
                             "program": Path(str(spec.get("command", ""))).name if spec.get("command") else None,
                             "supported": _transport(spec) != "unsupported" and name not in ("sharelet_public", "sharelet_images")}
                            for name, spec in sorted(table.items())],
                "credentials_copied": False}

    def save_connections(self, body):
        if (not isinstance(body, dict) or set(body) != {"path", "servers"}
                or not isinstance(body["servers"], list) or len(body["servers"]) > 8
                or any(not isinstance(name, str) for name in body["servers"])
                or len(set(body["servers"])) != len(body["servers"])):
            raise CapsuleError("Choose up to eight servers from one existing MCP configuration.")
        table = _server_table(body["path"])
        if any(name in ("sharelet_public", "sharelet_images") for name in body["servers"]):
            raise CapsuleError("The sharelet_public and sharelet_images names are reserved for built-in tools. Rename this connection in its source configuration.")
        if any(name not in table or _transport(table[name]) == "unsupported" for name in body["servers"]):
            raise CapsuleError("A selected MCP server is unavailable or uses an unsupported transport.")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_private(self.path, {"import": {"path": str(Path(body["path"]).expanduser().resolve()),
                                             "servers": body["servers"]}})
        self.refresh()
        return self.connection_status()

    def _ensure_loop(self):
        if self._loop:
            return
        loop = asyncio.new_event_loop()
        self._loop = loop
        def run():
            asyncio.set_event_loop(loop)
            loop.run_forever()
            loop.close()
        self._thread = threading.Thread(target=run, daemon=True, name="sharelet-mcp-connections")
        self._thread.start()

    def refresh(self):
        with self._lock:
            if self._closed:
                return self.connection_status()
            self._signature = self._stamp()
            desired = {}
            try:
                imported = self._settings().get("import", {})
                if imported:
                    if not isinstance(imported, dict):
                        raise ValueError("Invalid import")
                    table = _server_table(imported.get("path", ""))
                    selected = imported.get("servers", [])
                    if not isinstance(selected, list):
                        raise ValueError("Invalid server selection")
                    desired = {name: table[name] for name in selected if name in table}
            except Exception:
                desired = {}
            for name, current in list(self._connections.items()):
                spec = desired.get(name)
                fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest() if spec else None
                if spec is None or fingerprint != current["fingerprint"] or current["state"] == "unavailable":
                    self._connections.pop(name)
                    if self._loop and current.get("queue"):
                        self._loop.call_soon_threadsafe(current["queue"].put_nowait, None)
                    elif current.get("task"):
                        self._loop.call_soon_threadsafe(current["task"].cancel)
            for name, spec in desired.items():
                if name in self._connections:
                    continue
                self._ensure_loop()
                current = {"state": "connecting", "tools": [], "queue": None, "task": None,
                           "transport": _transport(spec),
                           "fingerprint": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()}
                self._connections[name] = current
                self._loop.call_soon_threadsafe(self._launch, name, spec, current)
        return self.connection_status()

    def _launch(self, name, spec, current):
        current["task"] = asyncio.create_task(self._worker(name, spec, current))

    async def _worker(self, name, spec, current):
        # One long-lived task owns all anyio scopes and session cleanup.
        from datetime import timedelta
        try:
            from mcp import ClientSession, StdioServerParameters
            async with AsyncExitStack() as stack:
                if _transport(spec) == "stdio":
                    from mcp.client.stdio import stdio_client
                    command = _expand(spec["command"])
                    arguments = spec.get("args", [])
                    if not isinstance(arguments, list) or any(not isinstance(arg, str) for arg in arguments):
                        raise ValueError("Invalid MCP arguments")
                    environment = {key: _expand(value) for key, value in spec.get("env", {}).items()
                                   if isinstance(key, str)}
                    for key in spec.get("env_vars", []):
                        if not isinstance(key, str) or key not in os.environ:
                            raise ValueError("Missing MCP environment")
                        environment[key] = os.environ[key]
                    params = StdioServerParameters(command=command, args=[_expand(arg) for arg in arguments],
                                                   env=environment, cwd=spec.get("cwd"))
                    # Child diagnostics can contain provider credentials. Never relay them.
                    log = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
                    read, write = await stack.enter_async_context(stdio_client(params, errlog=log))
                elif _transport(spec) == "http":
                    import httpx
                    from mcp.client.streamable_http import streamable_http_client
                    url = _expand(spec["url"])
                    parsed = urlsplit(url)
                    if (parsed.username or parsed.password or parsed.fragment
                            or parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"))):
                        raise ValueError("Use HTTPS or a loopback MCP endpoint")
                    headers = {key: _expand(value) for key, value in spec.get("headers", spec.get("http_headers", {})).items()}
                    token_env = spec.get("bearer_token_env_var")
                    if token_env:
                        if token_env not in os.environ:
                            raise ValueError("Missing MCP token environment")
                        headers["Authorization"] = "Bearer " + os.environ[token_env]
                    for header, variable in spec.get("env_http_headers", {}).items():
                        headers[header] = os.environ[variable]
                    http = await stack.enter_async_context(httpx.AsyncClient(headers=headers,
                        timeout=httpx.Timeout(30,read=190),follow_redirects=False))
                    read, write, _ = await stack.enter_async_context(streamable_http_client(url,http_client=http))
                else:
                    raise ValueError("Unsupported MCP transport")
                session = await stack.enter_async_context(ClientSession(read, write,
                                                                       read_timeout_seconds=timedelta(seconds=180)))
                await asyncio.wait_for(session.initialize(), timeout=25)
                rows, cursor, seen = [], None, set()
                for _ in range(20):
                    result = await asyncio.wait_for(session.list_tools(cursor=cursor), timeout=20)
                    for tool in result.tools:
                        if tool.name in CONTROL_TOOLS or tool.name in UTILITY_TOOLS:
                            continue
                        annotations = tool.annotations.model_dump(exclude_none=True) if tool.annotations else None
                        entry = tool_binding(name, tool.name, tool.inputSchema,
                                                 title=getattr(tool, "title", None) or tool.name,
                                                 description=tool.description or "", annotations=annotations)
                        entry["source_revision"] = current["fingerprint"]
                        rows.append(entry)
                    cursor = result.nextCursor
                    if not cursor:
                        break
                    if cursor in seen:
                        raise ValueError("Repeated MCP cursor")
                    seen.add(cursor)
                else:
                    raise ValueError("Too many MCP tools")
                queue = asyncio.Queue()
                with self._lock:
                    if self._connections.get(name) is not current:
                        return
                    current.update(state="ready", tools=rows, queue=queue)
                while True:
                    request = await queue.get()
                    if request is None:
                        return
                    tool, arguments, future, guard = request
                    if not future.set_running_or_notify_cancel():
                        continue
                    current["in_flight"] = future
                    try:
                        if guard:
                            guard()
                        result = await session.call_tool(tool, arguments)
                        if result.isError:
                            value = {"ok": False}
                        else:
                            value = {"ok": True, "result": result.model_dump(exclude_none=True)}
                            if result.structuredContent is not None:
                                value["structuredContent"] = result.structuredContent
                        if not future.done():
                            future.set_result(value)
                    except DispatchNotSent as error:
                        if not future.done():
                            future.set_exception(error)
                    except Exception:
                        if not future.done():
                            future.set_result({"ok": False})
                    finally:
                        current["in_flight"] = None
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            with self._lock:
                queue = current.get("queue")
                in_flight = current.get("in_flight")
                if in_flight and not in_flight.done():
                    in_flight.set_result({"ok": False})
                if self._connections.get(name) is current:
                    current.update(state="unavailable", tools=[], queue=None)
                if queue:
                    while not queue.empty():
                        item = queue.get_nowait()
                        if item is not None and not item[2].done():
                            item[2].set_result({"ok": False})

    def catalog(self):
        self._ensure_current()
        entries = basic_bindings() if self.builtin else []
        if self.images:
            entries.extend(image_bindings())
        with self._lock:
            for current in self._connections.values():
                if current["state"] == "ready":
                    entries.extend(current["tools"])
        # The built-in namespace is reserved. Importing a namesake cannot override it.
        unique = {}
        for entry in entries:
            unique.setdefault(entry["id"], entry)
        return list(unique.values())

    def call(self, server, tool, arguments):
        self._ensure_current()
        guard = current_dispatch_guard()
        if self.builtin and server == "sharelet_public":
            if guard:
                guard()
            return direct_dispatch(server, tool, arguments)
        if self.images and server == "sharelet_images" and tool == "generate_avatar":
            try:
                from .image_provider import generate_avatar
            except ImportError:
                from image_provider import generate_avatar
            if guard:
                guard()
            return {"ok": True, "result": generate_avatar(**arguments, settings_path=self.root / "provider.json", secret_reader=self._secret_reader)}
        with self._lock:
            current = self._connections.get(server)
            available = current and current["state"] == "ready" and any(
                item["tool"] == tool and item["supported"] for item in current["tools"])
            if not available:
                return {"ok": False}
            queue = current["queue"]
        future = Future()
        self._loop.call_soon_threadsafe(queue.put_nowait, (tool, arguments, future, guard))
        try:
            return future.result(timeout=190)
        except TimeoutError:
            # A request cancelled before the session picked it up was never sent.
            if future.cancel():
                return {'ok':False,'not_sent':True}
            return {'ok':False,'uncertain':True}
        except DispatchNotSent:
            raise
        except Exception:
            # Never turn a lost response into a fresh execution.
            return {"ok": False}

    def connection_status(self):
        with self._lock:
            return {"mode": "portable", "import_supported": True,
                    "servers": [{"name": name, "status": current["state"], "transport": current["transport"],
                                 "tool_count": len(current["tools"])}
                                for name, current in sorted(self._connections.items())],
                    "message": "Selected connections use your existing configuration. Credentials are not copied."}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            current = list(self._connections.values())
            loop = self._loop
        if not loop:
            return
        async def shutdown():
            tasks = [item["task"] for item in current if item.get("task")]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
        try:
            future.result(timeout=8)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        self._thread.join(timeout=3)
