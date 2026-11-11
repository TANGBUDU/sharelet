"""Build the public plugin download from an explicit source allowlist."""
from io import BytesIO
from pathlib import Path
import zipfile

FILES = (
    "__init__.py", "app.py", "catalog.py", "native_tools.py", "connections.py", "owner_client.py", "sharelet_mcp.py", "sharing.py",
    "public_tools.py", "demo_mcp.py", "distribution.py", "host.py", "image_provider.py",
    "jobs.py", "media.py", "file_inputs.py", "presets.py", "service.py", "sharelet.py", "tunnel.py", "relay.py",
    "webserver.py", "demo_image_mcp.py", "plugin.yaml", "README.md", "README.zh-CN.md", "README.ja.md",
    "VALIDATION.md", "requirements-preview.txt", "Start-Sharelet.ps1", "LICENSE",
    "web/app.js", "web/i18n.js", "web/style.css", "web/index.html", "web/guest.html",
    "web/weather.js", "web/weather.css", "web/files.js", "web/files.css", "web/forms.js", "web/owner.js", "web/guest.js", "web/canvas-demo.js",
    "examples/permission_canvas/sandbox.py", "examples/permission_canvas/server.py", "examples/permission_canvas/README.md",
    "examples/pdf_workshop/server.py", "examples/pdf_workshop/worker.py", "examples/pdf_workshop/requirements.txt", "examples/pdf_workshop/README.md",
    "web/icon.svg",
)


def build_plugin_archive(root=None):
    root = Path(root or Path(__file__).parent).resolve()
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            source = root / name
            if (source.is_symlink() or not source.is_file()
                    or source.resolve().parent != (root / name).parent.resolve()
                    or not source.resolve().is_relative_to(root)):
                raise FileNotFoundError("An installable source file is unavailable")
            archive.writestr("sharelet/" + name, source.read_bytes())
    return output.getvalue()
