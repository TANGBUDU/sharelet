"""Reviewed scalar-form catalog shared by Hermes and portable MCP adapters."""
from copy import deepcopy
import hashlib
import json
import re

try:
    from .sharelet import CapsuleError, normalise_schema
except ImportError:
    from sharelet import CapsuleError, normalise_schema

CONTROL_TOOLS = frozenset({
    "sharelet_status", "sharelet_list_tools", "sharelet_create_share",
    "sharelet_list_shares", "sharelet_revoke_share", "sharelet_connection_status",
})
UTILITY_TOOLS = frozenset({"list_resources", "read_resource", "list_prompts", "get_prompt",
                           "list_event_types", "subscribe_event", "unsubscribe_event"})


def contract_digest(schema):
    return hashlib.sha256(json.dumps(normalise_schema(schema), sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def tool_binding(server, tool, schema, *, title=None, description="", annotations=None):
    entry = {"id": server + "::" + tool, "server": server, "tool": tool, "kind": "tool",
             "title": (title or tool).replace("_", " ")[:80],
             "description": str(description or "Run a tool selected by the creator.")[:800],
             "schema": {}, "locked_args": {}, "supported": False, "icon": "sliders",
             "color": "mint", "source": server}
    if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", server)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", tool)):
        entry["unsupported_reason"] = "This tool identity cannot be shared."
        return entry
    if tool in CONTROL_TOOLS:
        entry["unsupported_reason"] = "Sharelet management tools cannot be shared."
        return entry
    if isinstance(schema, dict) and any(re.search(r"(?:api.?key|password|secret|authorization|access.?token)", name, re.I)
                                        for name in schema.get("properties", {}) if isinstance(name, str)):
        entry["unsupported_reason"] = "Configure credentials on the MCP server, rather than exposing them as guest form fields."
        return entry
    hints = annotations if isinstance(annotations, dict) else {}
    entry["requires_side_effects_approval"] = hints.get("readOnlyHint") is not True
    entry["safety"] = "review_required" if entry["requires_side_effects_approval"] else "read_only"
    if hints.get("destructiveHint") is True:
        entry["safety"] = "blocked"
        entry["unsupported_reason"] = "Destructive tools cannot be shared."
        return entry
    try:
        entry["schema"] = normalise_schema(deepcopy(schema))
        entry["contract_hash"] = contract_digest(entry["schema"])
        entry["review_hash"] = review_digest(entry)
        entry["supported"] = True
    except CapsuleError as exc:
        entry["unsupported_reason"] = str(exc)
    return entry


def review_digest(entry):
    """Bind a human decision to the tool identity, description, input and risk."""
    fields = ("id", "server", "tool", "kind", "title", "description", "schema",
              "safety", "requires_side_effects_approval", "source_revision", "locked_args")
    return hashlib.sha256(json.dumps({key: entry.get(key) for key in fields}, sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def reviewed_catalog(entries):
    result = deepcopy(entries)
    for entry in result:
        if entry.get("kind") == "image" and "safety" not in entry:
            entry.update(safety="image_recipe", requires_side_effects_approval=False)
        elif "safety" not in entry:
            requires = entry.get("read_only") is not True
            entry.update(safety="review_required" if requires else "read_only",
                         requires_side_effects_approval=requires)
        entry["review_hash"] = review_digest(entry)
    return result
