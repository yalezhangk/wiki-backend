from __future__ import annotations

import re
import unicodedata
from pathlib import Path


def normalize_document_name(filename: str) -> str:
    """返回用于全局 Ingest 重名判断的稳定文件主名键。"""
    value = unicodedata.normalize("NFKC", Path(filename).stem)
    return re.sub(r"\s+", " ", value.strip()).casefold()
