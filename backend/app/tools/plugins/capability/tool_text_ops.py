from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, Field

from app.tools.models import PluginMetadata, ToolContext, ToolRegistration


PLUGIN_NAME = "text_ops"
PLUGIN_VERSION = "0.1.0"
PLUGIN_META = PluginMetadata(
    name=PLUGIN_NAME,
    layer="capability",
    version=PLUGIN_VERSION,
    description="Deterministic text normalization tools for agent workflows.",
    use_cases=["Convert user-provided text into a stable ASCII slug."],
    tags={"text", "normalization"},
    safety={"has_write_tools": False, "max_safety_level": "compute"},
)


class SlugifyArgs(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)


async def slugify(args: BaseModel, _context: ToolContext) -> dict:
    values = SlugifyArgs.model_validate(args)
    normalized = unicodedata.normalize("NFKD", values.text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return {"slug": slug}


PLUGIN_TOOLS = [
    ToolRegistration(
        func=slugify,
        name="capability_text_slugify",
        description="Convert text into a normalized ASCII slug.",
        args_model=SlugifyArgs,
        layer="capability",
        plugin_name=PLUGIN_NAME,
        version=PLUGIN_VERSION,
        safety_level="compute",
        visibility="user",
    ),
]
