from __future__ import annotations

import base64
import mimetypes
import json
from pathlib import Path
import re
from typing import Any

import requests

from .config import AppConfig


def _parse_json_response(text: str) -> Any:
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


class OpenAICompatibleProvider:
    def __init__(self, config: AppConfig):
        self.config = config
        self.base_url = (config.openai_compat_base_url or "").rstrip("/")
        self.timeout = config.openai_compat_timeout_seconds
        self.session = requests.Session()
        self.headers = {"Content-Type": "application/json"}
        if config.openai_compat_api_key:
            self.headers["Authorization"] = f"Bearer {config.openai_compat_api_key}"

    def supports_web_search(self) -> bool:
        return False

    def supports_vision(self) -> bool:
        return bool(self.config.vision_model_name)

    def supports_image_generation(self) -> bool:
        return bool(self.config.ai_image_model)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}{path}",
            headers=self.headers,
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def _extract_text(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError(f"No choices returned: {payload}")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            return "\n".join(str(part.get("text", "")) for part in content if part.get("text")).strip()
        return str(content).strip()

    def generate_text(
        self,
        model: str,
        prompt: str,
        temperature: float = 0.6,
        use_web_search: bool = False,
    ) -> str:
        _ = use_web_search
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        return self._extract_text(self._post("/chat/completions", payload))

    def generate_json(
        self,
        model: str,
        prompt: str,
        temperature: float = 0.4,
        use_web_search: bool = False,
    ) -> Any:
        return _parse_json_response(
            self.generate_text(
                model=model,
                prompt=prompt,
                temperature=temperature,
                use_web_search=use_web_search,
            )
        )

    def generate_text_with_images(
        self,
        model: str,
        prompt: str,
        image_paths: list[Path],
        temperature: float = 0.4,
        use_web_search: bool = False,
    ) -> str:
        _ = use_web_search
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image_path in image_paths:
            mime_type, _encoding = mimetypes.guess_type(image_path.name)
            mime_type = mime_type or "image/png"
            encoded = base64.b64encode(image_path.read_bytes()).decode("utf-8")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                }
            )
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
        }
        return self._extract_text(self._post("/chat/completions", payload))

    def generate_json_with_images(
        self,
        model: str,
        prompt: str,
        image_paths: list[Path],
        temperature: float = 0.3,
        use_web_search: bool = False,
    ) -> Any:
        return _parse_json_response(
            self.generate_text_with_images(
                model=model,
                prompt=prompt,
                image_paths=image_paths,
                temperature=temperature,
                use_web_search=use_web_search,
            )
        )

    def generate_image(self, model: str, prompt: str, output_path: Path) -> Path:
        payload = {
            "model": model,
            "prompt": prompt,
            "size": "1024x1024",
        }
        data = self._post("/images/generations", payload)
        items = data.get("data") or []
        if not items:
            raise RuntimeError(f"No image candidates returned: {data}")
        first = items[0]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if first.get("b64_json"):
            output_path.write_bytes(base64.b64decode(first["b64_json"]))
            return output_path
        if first.get("url"):
            response = self.session.get(str(first["url"]), timeout=self.timeout)
            response.raise_for_status()
            output_path.write_bytes(response.content)
            return output_path
        raise RuntimeError(f"Unsupported image response: {data}")
