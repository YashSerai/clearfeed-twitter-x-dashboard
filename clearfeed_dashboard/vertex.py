from __future__ import annotations

import base64
import mimetypes
import time
from pathlib import Path
from typing import Any

import google.auth
from google.auth.transport.requests import AuthorizedSession
from requests import exceptions as requests_exceptions

from .config import AppConfig


def _parse_json_response(text: str) -> Any:
    from .providers import parse_json_response

    return parse_json_response(text)


class VertexProvider:
    def __init__(self, config: AppConfig):
        self.config = config
        credentials, _project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self.session = AuthorizedSession(credentials)
        self.timeout_seconds = max(1, int(getattr(config, "vertex_timeout_seconds", 240)))
        self.max_retries = max(0, int(getattr(config, "vertex_max_retries", 2)))

    def supports_web_search(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        return bool(self.config.vision_model_name)

    def supports_image_generation(self) -> bool:
        return bool(self.config.ai_image_model)

    def _endpoint(self, model: str, method: str = "generateContent") -> str:
        return (
            f"https://aiplatform.googleapis.com/v1/projects/{self.config.google_cloud_project}"
            f"/locations/{self.config.google_cloud_location}/publishers/google/models/{model}:{method}"
        )

    def _generate_text_parts(
        self,
        model: str,
        parts: list[dict[str, Any]],
        temperature: float = 0.6,
        use_web_search: bool = False,
    ) -> str:
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": temperature},
        }
        if use_web_search:
            payload["tools"] = [{"googleSearch": {}}]
        data = self._post_json(self._endpoint(model), payload)
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"No candidates returned: {data}")
        parts = candidates[0].get("content", {}).get("parts", [])
        texts = [part.get("text", "") for part in parts if part.get("text")]
        return "\n".join(texts).strip()

    def _post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(endpoint, json=payload, timeout=self.timeout_seconds)
                response.raise_for_status()
                return response.json()
            except (
                requests_exceptions.ReadTimeout,
                requests_exceptions.ConnectTimeout,
                requests_exceptions.Timeout,
                requests_exceptions.ConnectionError,
            ) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(
            "Vertex request failed after "
            f"{self.max_retries + 1} attempt(s). "
            f"Last error: {last_error}"
        ) from last_error

    def generate_text(
        self,
        model: str,
        prompt: str,
        temperature: float = 0.6,
        use_web_search: bool = False,
    ) -> str:
        return self._generate_text_parts(model=model, parts=[{"text": prompt}], temperature=temperature, use_web_search=use_web_search)

    def generate_json(
        self,
        model: str,
        prompt: str,
        temperature: float = 0.4,
        use_web_search: bool = False,
    ) -> Any:
        return _parse_json_response(self.generate_text(model=model, prompt=prompt, temperature=temperature, use_web_search=use_web_search))

    def generate_text_with_images(
        self,
        model: str,
        prompt: str,
        image_paths: list[Path],
        temperature: float = 0.4,
        use_web_search: bool = False,
    ) -> str:
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for image_path in image_paths:
            mime_type, _ = mimetypes.guess_type(image_path.name)
            mime_type = mime_type or "image/png"
            parts.append(
                {
                    "inlineData": {
                        "mimeType": mime_type,
                        "data": base64.b64encode(image_path.read_bytes()).decode("utf-8"),
                    }
                }
            )
        return self._generate_text_parts(model=model, parts=parts, temperature=temperature, use_web_search=use_web_search)

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
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.8, "responseModalities": ["TEXT", "IMAGE"]},
        }
        data = self._post_json(self._endpoint(model), payload)
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"No image candidates returned: {data}")
        for part in candidates[0].get("content", {}).get("parts", []):
            inline = part.get("inlineData")
            if not inline:
                continue
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(base64.b64decode(inline["data"]))
            return output_path
        raise RuntimeError(f"No inline image returned: {data}")
