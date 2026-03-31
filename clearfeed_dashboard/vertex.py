from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

import google.auth
from google.auth.transport.requests import AuthorizedSession

from .config import AppConfig


def _parse_json_response(text: str) -> Any:
    from .providers import parse_json_response

    return parse_json_response(text)


class VertexProvider:
    def __init__(self, config: AppConfig):
        self.config = config
        credentials, _project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self.session = AuthorizedSession(credentials)

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
        response = self.session.post(self._endpoint(model), json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"No candidates returned: {data}")
        parts = candidates[0].get("content", {}).get("parts", [])
        texts = [part.get("text", "") for part in parts if part.get("text")]
        return "\n".join(texts).strip()

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
        response = self.session.post(self._endpoint(model), json=payload, timeout=180)
        response.raise_for_status()
        data = response.json()
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
