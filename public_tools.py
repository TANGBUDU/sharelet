"""Two real, no-credential sample tools. No model or private filesystem access."""
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_SCHEMA = {"type": "object", "properties": {
    "repo": {"type": "string", "title": "GitHub 仓库", "description": "填写 owner/repo，例如 NousResearch/hermes-agent", "minLength": 3, "maxLength": 100},
    "language": {"type": "string", "title": "显示语言", "enum": ["中文", "English"], "default": "中文"}
}, "required": ["repo"], "additionalProperties": False}
READ_SCHEMA = {"type": "object", "properties": {
    "text": {"type": "string", "title": "Text to read", "description": "Paste a short passage to estimate its reading time.", "minLength": 1, "maxLength": 500},
    "speed": {"type": "integer", "title": "Reading units per minute", "minimum": 100, "maximum": 1000, "default": 300}
}, "required": ["text"], "additionalProperties": False}

CLEAN_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "text": {"type": "string", "title": "Your text", "minLength": 1, "maxLength": 500},
    "mode": {"type": "string", "title": "Letter case", "enum": ["keep", "upper", "lower"], "default": "keep"},
    "preserve_lines": {"type": "boolean", "title": "Keep line breaks", "default": True},
}, "required": ["text"]}


def repo_brief(repo: str, language: str = "中文") -> dict:
    repo = repo.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,50}/[A-Za-z0-9_.-]{1,70}", repo):
        raise ValueError("请输入 owner/repo 格式的公开仓库名称。")
    request = Request("https://api.github.com/repos/" + repo,
                      headers={"User-Agent": "Sharelet/0.2 public-demo", "Accept": "application/vnd.github+json"})
    try:
        with urlopen(request, timeout=8) as response:
            info = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return {"kind": "notice", "message": "没有找到这个公开仓库，请检查名称。"}
        if exc.code in (403, 429):
            return {"kind": "notice", "message": "GitHub 的公开查询额度暂时用完，请稍后重试。"}
        raise RuntimeError("GitHub 查询暂时不可用。") from None
    except (URLError, TimeoutError):
        raise RuntimeError("GitHub 查询暂时不可用。") from None
    # Explicit public-field projection, never forward raw provider metadata or headers.
    return {"kind": "repository", "name": info.get("full_name", repo),
            "description": info.get("description") or ("No description provided." if language == "English" else "这个仓库暂时没有简介。"),
            "stars": info.get("stargazers_count", 0), "forks": info.get("forks_count", 0),
            "language": info.get("language") or "—", "license": (info.get("license") or {}).get("spdx_id") or "—",
            "updated_at": info.get("pushed_at"), "url": info.get("html_url"),
            "topics": (info.get("topics") or [])[:8], "source": "GitHub Public API"}


def reading_time(text: str, speed: int = 300) -> dict:
    import math
    # Count CJK characters individually; count Latin word runs once.
    units = len(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+", text))
    return {"kind": "reading", "units": units, "seconds": max(1, math.ceil(units / speed * 60)),
            "speed": speed, "message": "An estimate based on the chosen reading speed."}


def text_cleanup(text: str, mode: str = "keep", preserve_lines: bool = True) -> dict:
    if mode not in ("keep", "upper", "lower") or type(preserve_lines) is not bool:
        raise ValueError("Invalid cleanup options")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    cleaned = ("\n" if preserve_lines else " ").join(line for line in lines if line).strip()
    if mode == "upper":cleaned = cleaned.upper()
    if mode == "lower":cleaned = cleaned.lower()
    return {"text": cleaned, "characters": len(cleaned), "lines": len(cleaned.splitlines())}


SAMPLES = [
    {"id": "sharelet_public::repo_brief", "server": "sharelet_public", "tool": "repo_brief", "title": "开源项目速览",
     "description": "输入 GitHub 仓库名，查看简介、Stars、主要语言与许可证。", "schema": REPO_SCHEMA,
     "icon": "code", "read_only": True, "supported": True},
    {"id": "sharelet_public::reading_time", "server": "sharelet_public", "tool": "reading_time", "title": "阅读时间计算",
     "description": "粘贴一段文字，快速估算需要读多久。", "schema": READ_SCHEMA,
     "icon": "book", "read_only": True, "supported": True},
]


def direct_dispatch(server, tool, args):
    if server != "sharelet_public" or tool not in ("repo_brief", "reading_time", "text_cleanup"):
        return {"ok": False, "error": "unknown tool"}
    return {"ok": True, "result": {"repo_brief": repo_brief, "reading_time": reading_time, "text_cleanup": text_cleanup}[tool](**args)}


def basic_bindings():
    try:
        from .catalog import tool_binding
    except ImportError:
        from catalog import tool_binding
    return [tool_binding("sharelet_public", "text_cleanup", CLEAN_SCHEMA, title="Text cleanup",
                         description="Clean spacing, choose letter case and keep the line breaks you want. Runs locally, with no model call.", annotations={"readOnlyHint": True, "destructiveHint": False}),
            tool_binding("sharelet_public", "reading_time", READ_SCHEMA, title="Reading time",
                         description="Estimate reading time for a passage. The creator can fix the reading speed. Runs locally, with no model call.", annotations={"readOnlyHint": True, "destructiveHint": False})]
