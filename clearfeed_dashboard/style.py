from __future__ import annotations

from pathlib import Path


def load_style_packet(paths: list[Path]) -> str:
    chunks: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        chunks.append(f"## {path.name}\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(chunks)
