from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Load a versioned backend prompt by filename."""
    prompt_name = Path(name)
    if prompt_name.name != name or prompt_name.suffix != ".md":
        raise ValueError(f"invalid prompt name: {name}")
    return (PROMPTS_DIR / prompt_name).read_text(encoding="utf-8")


def render_prompt(name: str, **values: str) -> str:
    """Render a prompt using string.Template placeholders."""
    return Template(load_prompt(name)).substitute(values)
