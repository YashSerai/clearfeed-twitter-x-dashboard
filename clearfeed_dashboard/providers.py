from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

from .config import AppConfig
from .openai_compatible import OpenAICompatibleProvider
from .vertex import VertexProvider


class AIProvider(Protocol):
    def generate_text(
        self,
        model: str,
        prompt: str,
        temperature: float = 0.6,
        use_web_search: bool = False,
    ) -> str: ...

    def generate_json(
        self,
        model: str,
        prompt: str,
        temperature: float = 0.4,
        use_web_search: bool = False,
    ) -> Any: ...

    def generate_text_with_images(
        self,
        model: str,
        prompt: str,
        image_paths: list[Path],
        temperature: float = 0.4,
        use_web_search: bool = False,
    ) -> str: ...

    def generate_json_with_images(
        self,
        model: str,
        prompt: str,
        image_paths: list[Path],
        temperature: float = 0.3,
        use_web_search: bool = False,
    ) -> Any: ...

    def generate_image(self, model: str, prompt: str, output_path: Path) -> Path: ...

    def supports_web_search(self) -> bool: ...

    def supports_vision(self) -> bool: ...

    def supports_image_generation(self) -> bool: ...


def parse_json_response(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(1))


def build_provider(config: AppConfig) -> AIProvider:
    if config.ai_provider == "openai_compatible":
        return OpenAICompatibleProvider(config)
    return VertexProvider(config)
