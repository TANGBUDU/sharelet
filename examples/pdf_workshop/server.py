"""An actual local PDF tool exposed over stdio MCP, without models or API keys."""
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Annotated,Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

mcp=FastMCP('PDF workshop')


@mcp.tool(description='Merge PDFs, extract or reorder pages, or compress PDFs locally. Uploaded source files stay unchanged. No model or paid API is used.',
          annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,openWorldHint=False))
def process_pdf(
    files:Annotated[str,Field(title='PDF files',description='Upload ordinary PDFs. Encrypted files, signatures and interactive forms are not supported.',
                             min_length=2,max_length=500,json_schema_extra={'format':'sharelet-pdf-uploads'})],
    action:Literal['Merge PDFs','Extract pages','Lossless compression']='Merge PDFs',
    pages:Annotated[str,Field(title='Pages to keep',description='For extraction only. For example: 1-3,5 or 3,1-2.',max_length=200)]='',
) -> dict:
    """Merge PDFs, extract or reorder selected pages, or compress page streams locally.
    Receives uploaded file IDs from the shared file workspace and returns a downloadable PDF.
    No model, account, paid API or external website is used. Source files stay unchanged.
    """
    try:
        response=subprocess.run([sys.executable,str(Path(__file__).with_name('worker.py'))],
            input=json.dumps({'files':files,'action':action,'pages':pages}).encode(),
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=35,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if response.returncode or len(response.stdout)>4096:return {'kind':'file_error','code':'processing_limit'}
        return json.loads(response.stdout)
    except (subprocess.TimeoutExpired,ValueError):
        return {'kind':'file_error','code':'processing_limit'}


if __name__=='__main__':
    root=Path(os.environ.get('SHARELET_FILE_WORKSPACE',''))
    if not root.is_absolute() or not root.is_dir():raise RuntimeError('Set SHARELET_FILE_WORKSPACE to this Sharelet workspace.')
    import pypdf  # Fail discovery visibly if the optional dependency is missing.
    mcp.run(transport='stdio')
