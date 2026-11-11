# Share a useful PDF tool

Guests upload their own PDFs, choose merge, extraction/reordering or lossless compression, and download the actual processed PDF. The optional MCP server uses **pypdf 7.0.1** on the creator's machine. It uses no model, paid API, external website or developer credentials.

## Run it

Install `requirements.txt` from this directory into the Python environment used for the MCP server. Run Sharelet normally. In a separate existing MCP configuration add:

```json
{
  "mcpServers": {
    "pdf_workshop": {
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/sharelet/examples/pdf_workshop/server.py"],
      "env": {
        "SHARELET_FILE_WORKSPACE": "/absolute/path/to/this/sharelet/workspace"
      }
    }
  }
}
```

`SHARELET_FILE_WORKSPACE` is the **exact data directory** of the Sharelet service. It contains `service.json`, `file-uploads` and `file-outbox`; it is not the source directory. Select this server in Sharelet's Connection panel. In native Hermes mode, configure and explicitly allow this MCP server in the active profile instead.

Choose **Process PDF**, keep file uploads open to guests, set the allowance and expiry, review the tool (it creates output files), and share the link. You may fix `action` to offer a dedicated merge or extraction link. Guests can supply only uploaded IDs belonging to this share, never a host file path. Files cannot be fixed in a share definition.

## What it supports

- Merge 2–8 PDFs in the order chosen by the guest.
- Extract pages from one PDF, including reordering or repeating pages: `1-3,5`, `3,1-2`, `3-1`, `1,1`.
- Compress one PDF's page content streams with lossless Flate compression and deduplicate objects. Already optimized PDFs may remain the same size or become larger; the result reports this honestly. Images are not downsampled.
- Up to 5 MB per file, 20 MB total input and 200 input/output pages. The output limit is 25 MB. Encrypted, signed and interactive-form PDFs are refused. Page operations produce a new PDF and are not intended to preserve document-level bookmarks or interactive features.

The algorithm follows pypdf's [documented lossless compression methods](https://pypdf.readthedocs.io/en/stable/user/file-size.html). The library and its authors are credited; this example demonstrates sharing an existing PDF capability through MCP.

## File protocol and lifetime

The schema marks one scalar string with `format: sharelet-pdf-uploads`. Sharelet renders an upload picker and sends a bounded JSON string of opaque file IDs to the MCP tool. The HTTP layer checks share ownership, expiry and input locks before admitting a run. Uploaded sources are deleted after processing; unused uploads expire after 15 minutes. Results remain downloadable only while their original share is valid, with a 256 MB workspace storage ceiling.

The tool writes a new random asset ID into the workspace's fixed `file-outbox` and returns a `kind: file` receipt. Sharelet validates and copies that PDF into its private result store. Visitors receive a capability-scoped attachment URL; paths and outbox IDs are omitted. Arbitrary remote media and host paths are not accepted.

Each PDF is parsed in a subprocess with a 35-second parent deadline. Linux also applies memory, CPU and output-file limits. This is resource containment for the PDF parser, not a general OS sandbox for arbitrary MCP programs. Only connect tools you have reviewed.
