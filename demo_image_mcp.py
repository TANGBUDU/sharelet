"""Example image recipe served over real MCP. Credentials remain on the host."""
from typing import Annotated, Literal
from pydantic import Field
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from image_provider import generate_avatar as generate
from image_provider import ProviderSettings
from presets import MODEL, SIZE
import os
from pathlib import Path


def generate_avatar(subject:Annotated[str,Field(min_length=3,max_length=400)],
                    style:Literal["clay","anime","sticker"],
                    model:Literal["gpt-image-2"]="gpt-image-2",
                    size:Literal[1024]=1024,num_images:Literal[1]=1,
                    style_notes:Annotated[str,Field(max_length=300)]="")->dict:
    """Generate one image using a reviewed fixed recipe. This is a billed operation."""
    return generate(subject,style,model,size,num_images,style_notes)


mcp=FastMCP("Sharelet image recipes")
mcp.add_tool(generate_avatar,name="generate_avatar",description="Generate exactly one 1024px character image using GPT Image 2 and an approved style. Billed operation; no arbitrary file, URL, model or image-count input.",
             annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,openWorldHint=True))

def recipe_info()->dict:
    """Non-billable connection check. Never returns a provider credential."""
    configured=os.environ.get('SHARELET_PROVIDER_CONFIG')
    settings=ProviderSettings(Path(configured).parent) if configured else None
    return {'ready':bool(settings and settings.public()['configured']),'model':MODEL,'size':SIZE,'num_images':1}

mcp.add_tool(recipe_info,name='recipe_info',description='Non-billable recipe connection status; contains no credentials.',
             annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
if __name__=="__main__":
    mcp.run(transport="stdio")
