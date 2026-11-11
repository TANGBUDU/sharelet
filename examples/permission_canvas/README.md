# Share your Permission Canvas sandbox

This example exposes the **unchanged installed Permission Canvas backend**, version 0.5.13 in the tested run, through a narrow MCP adapter. Sharelet gives visitors a browser form, six scenarios and a visual result.

Only the application is synthetic. Permission decisions, action dispatch, yellow approval requests, screenshot masking and accessibility-tree redaction run through the original `PermissionCanvasBackend`. The adapter never opens a native driver, clipboard, human approval panel or model service. A fresh in-memory application and approval store are created for each request. No approval operation is exposed.

Visitors can edit a fictional green note, attempt to change blue revenue, attempt a yellow invitation, try a hidden field, attempt clipboard copying, or inspect the redacted agent view. The output shows whether an operation reached the synthetic application. The hidden credential is a new random test string; no real account data is used.

## Connect the existing plugin

Use the Python environment with the Hermes public SDK and Permission Canvas dependencies installed (`Pillow`, `mcp` and `jsonschema`). Point `PERMISSION_CANVAS_SOURCE` to your installed Permission Canvas source; `PYTHONPATH` points to your Hermes source if it is not already importable.

```json
{
  "mcpServers": {
    "permission_canvas_demo": {
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/sharelet/examples/permission_canvas/server.py"],
      "env": {
        "PERMISSION_CANVAS_SOURCE": "/absolute/path/to/permission-canvas",
        "PYTHONPATH": "/absolute/path/to/hermes-agent"
      }
    }
  }
}
```

Import and explicitly select this server in Sharelet's **Connection** panel. In Hermes mode, add it to your own MCP configuration and Sharelet's `mcp_allowlist`.

Choose `try_permission_canvas`, review its tool settings, set the run allowance and expiry, and create a public share. If Hermes omits the MCP read-only annotation, Sharelet asks for the extra tool review. The adapter is confined to a fictional in-memory app; it cannot operate the host desktop. Its source fingerprint and original plugin version are included in every result.

The demo is a browser experience of the backend's protections. It does not turn the native Permission Canvas application into a remotely controlled desktop. To protect real computer use, install and configure the original plugin as its own README describes.

Creating the link and running these six scenarios make no model or paid API calls. Sharelet still counts admitted runs and enforces revocation and expiry.
