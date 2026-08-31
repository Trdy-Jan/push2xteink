from __future__ import annotations

from pathlib import Path

from ..models import Article


def build_epub(title: str, articles: list[Article], *, out_dir: Path) -> Path:
    raise NotImplementedError  # P2c Task 2
