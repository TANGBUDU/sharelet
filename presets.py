"""Explicit, reviewed bindings. No private Hermes registry introspection."""
from copy import deepcopy

MODEL = "gpt-image-2"
SIZE = 1024
STYLES = {
    "clay": {
        "title": "Clay companions", "subtitle": "Tiny characters. Big personality.",
        "description": "Turn an idea into a soft clay character, with warm lighting and a little charm.",
        "color": "peach", "emoji": "✦",
        "prefix": "A charming original character avatar made of soft matte clay, handcrafted miniature, rounded friendly shapes, beautiful warm studio lighting, centered portrait, pastel cream background, premium 3D illustration, no text, no watermark. Subject: ",
        "example": "A tiny fox astronaut with a sage green scarf",
    },
    "anime": {
        "title": "Anime portraits", "subtitle": "A character, all your own.",
        "description": "Create an original anime portrait with clean lines, expressive eyes and soft colors.",
        "color": "lavender", "emoji": "✧",
        "prefix": "An original anime character avatar, polished illustration, expressive eyes, beautiful clean linework, tasteful pastel color palette, soft light, centered head and shoulders, simple pale lavender background, no text, no watermark. Subject: ",
        "example": "A curious botanist with silver hair and round glasses",
    },
    "sticker": {
        "title": "Sticker studio", "subtitle": "A small idea worth keeping.",
        "description": "Make a playful character sticker with bold shapes and a clean white border.",
        "color": "mint", "emoji": "✺",
        "prefix": "A delightful original die-cut character sticker design, bold rounded graphic shapes, thick clean white outline, expressive face, crisp colorful vector-like illustration, centered on a plain warm white background, no lettering, no watermark. Subject: ",
        "example": "A sleepy orange cat holding a very large coffee",
    },
}


def image_bindings(server="sharelet_images", tool="generate_avatar"):
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "subject": {"type": "string", "title": "Describe your character", "minLength": 3, "maxLength": 400},
        "style": {"type": "string", "enum": list(STYLES), "maxLength": 20},
        "model": {"type": "string", "const": MODEL, "maxLength": 80},
        "size": {"type": "integer", "const": SIZE, "minimum": SIZE, "maximum": SIZE},
        "num_images": {"type": "integer", "const": 1, "minimum": 1, "maximum": 1},
        "style_notes": {"type": "string", "maxLength": 300, "default": ""},
    }, "required": ["subject", "style", "model", "size", "num_images"]}
    return [{"id": server + "::" + tool + "::" + key, "server": server, "tool": tool,
             "kind": "image", "title": item["title"], "description": item["description"],
             "subtitle": item["subtitle"], "example": item["example"], "color": item["color"],
             "style": key, "model": MODEL, "size": SIZE, "num_images": 1,
             "schema": deepcopy(schema), "locked_args": {"style": key, "model": MODEL, "size": SIZE, "num_images": 1, "style_notes": ""},
             "supported": True, "icon": "sparkles"} for key, item in STYLES.items()]
