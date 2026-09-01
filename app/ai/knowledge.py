"""Loader for prompts and product knowledge.

Prompts and product facts live in markdown files next to this module so they can
be edited (and reviewed in a diff) without touching Python. Files are cached
after first read; call `reload()` to pick up edits without a restart.

This is deliberately a flat file loader. If the knowledge base outgrows a couple
of documents, this is the single place that grows a retrieval step - nothing
else in the application knows where the text comes from.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"


@lru_cache(maxsize=32)
def _read(path_str: str) -> str:
    path = Path(path_str)
    if not path.is_file():
        logger.error("missing prompt/knowledge file", extra={"path": path_str})
        return ""
    return path.read_text(encoding="utf-8").strip()


def load_prompt(name: str) -> str:
    return _read(str(PROMPTS_DIR / f"{name}.md"))


def load_knowledge(name: str) -> str:
    return _read(str(KNOWLEDGE_DIR / f"{name}.md"))


def product_knowledge() -> str:
    """Everything the AI is allowed to treat as product fact."""
    parts = [load_knowledge("product"), load_knowledge("objections")]
    return "\n\n".join(part for part in parts if part)


def sales_behaviour() -> str:
    return load_prompt("sales_agent")


def reload() -> None:
    """Drop the cache so edited files take effect on the next request."""
    _read.cache_clear()
    logger.info("prompt and knowledge cache cleared")
