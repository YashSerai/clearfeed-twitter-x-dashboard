from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import AppConfig
from .providers import AIProvider, build_provider
from .types import CandidateDecision, DraftPayload


class DraftingEngine:
    def __init__(self, config: AppConfig, style_packet: str):
        self.config = config
        self.style_packet = style_packet
        self.provider: AIProvider = build_provider(config)

    def supports_vision(self) -> bool:
        return self.config.vision_enabled and self.provider.supports_vision()

    def supports_web_search(self) -> bool:
        return self.config.web_research_enabled and self.provider.supports_web_search()

    def supports_image_generation(self) -> bool:
        return self.config.image_generation_enabled and self.provider.supports_image_generation()

    def prioritize_candidates(self, candidates: list[dict[str, Any]]) -> list[CandidateDecision]:
        if not candidates:
            return []
        prompt = f"""
You are ranking X posts for a high-signal builder or operator.

Voice packet:
{self.style_packet}

Rules:
- Prefer fresh posts with real implications for builders, AI products, APIs, launches, or contrarian technical takes.
- If the source is the home timeline, prioritize tweets that feel early but already show traction: velocity, healthy engagement, second-degree social proof, or obvious launch energy.
- Reward strong breakout posts from accounts outside the curated lists when there is still a concrete reply angle.
- Prefer posts where a reply or quote reply can add a concrete angle.
- Avoid generic hype, pure memes, or crowded mega-account threads unless the angle is genuinely sharp.
- Recommended action must be one of: reply, quote_reply, watch.
- Return JSON array only.
- Each item must include: tweet_id, llm_score, recommended_action, why.

Candidates:
{json.dumps(candidates, indent=2)}
"""
        raw = self.provider.generate_json(self.config.ai_text_model, prompt, temperature=0.3)
        decisions: list[CandidateDecision] = []
        for item in raw:
            decisions.append(
                CandidateDecision(
                    tweet_id=str(item["tweet_id"]),
                    heuristic_score=0.0,
                    llm_score=float(item.get("llm_score", 0)),
                    total_score=0.0,
                    recommended_action=str(item.get("recommended_action", "watch")),
                    why=str(item.get("why", "")).strip(),
                )
            )
        return decisions

    def draft_candidate_reply(
        self,
        candidate: dict[str, Any],
        draft_type: str,
        tweet_context: str | None = None,
        image_context: str | None = None,
        article_context: str | None = None,
        user_guidance: str | None = None,
    ) -> DraftPayload:
        use_web_search = bool(candidate.get("linked_url")) and self.supports_web_search()
        prompt = f"""
You are drafting a {draft_type.replace('_', ' ')} for the user.

Voice packet:
{self.style_packet}

Target post:
{json.dumps(candidate, indent=2)}

Tweet thread / quote / reply context:
{tweet_context or "None"}

Tweet image context:
{image_context or "None"}

Expanded context from linked page:
{article_context or "None"}

User drafting guidance:
{user_guidance or "None"}

Live web research:
{"Available for this request. Use it when it materially improves specificity or accuracy." if use_web_search else "Not enabled for this request."}

Rules:
- Sound like the user, not a corporate account.
- Use the profile packet for taste and background, not as copy to repeat.
- Do not inject identity details, job titles, or background unless they are genuinely relevant to the point.
- The default is to sound like a sharp builder on X, not to self-introduce.
- No em dashes.
- Add one real thing: a reaction, question, mechanism, disagreement, concrete observation, or implication.
- Stay tightly anchored to the target post.
- Use at least one concrete detail from the target tweet or surrounding context when possible.
- If Expanded context from linked page is present, treat it as primary grounding material.
- If Live web research is available, use it to understand the broader situation around the linked page, launch, claim, or company before drafting.
- If User drafting guidance is present, treat it as the steering brief unless it would make the reply inaccurate.
- If the post is thin, do not force a grand thesis.
- Before finalizing, check whether the draft could be pasted under a different AI tweet with almost no changes. If yes, rewrite it to be more specific.
- Favor language that sounds like a person reacting in-feed, not a polished mini-essay.
- Do not flatter the original poster.
- Keep it concise enough for X.
- Suggest an image only if a simple diagram or technical explainer visual would materially help.
- Internally draft 3 candidate replies, pressure-test them for specificity and voice, then return only the strongest final option.
- Return JSON only with keys: text, rationale, image_prompt, image_reason.
"""
        payload = self.provider.generate_json(
            self.config.ai_text_model,
            prompt,
            temperature=0.55,
            use_web_search=use_web_search,
        )
        return DraftPayload(
            draft_type=draft_type,
            text=str(payload["text"]).strip(),
            rationale=str(payload.get("rationale", "")).strip(),
            image_prompt=(str(payload["image_prompt"]).strip() if payload.get("image_prompt") else None),
            image_reason=(str(payload["image_reason"]).strip() if payload.get("image_reason") else None),
        )

    def summarize_tweet_images(self, candidate: dict[str, Any], image_paths: list[Path]) -> str:
        if not image_paths or not self.supports_vision() or not self.config.vision_model_name:
            return ""
        prompt = f"""
You are analyzing attached tweet images before drafting an X reply.

Target tweet metadata:
{json.dumps(candidate, indent=2)}

Rules:
- Describe only what materially affects the response.
- Focus on screenshots, charts, product UI, diagrams, benchmark tables, or text visible in the image.
- Keep it concise.
- Return JSON only with keys: summary, implications.
"""
        payload = self.provider.generate_json_with_images(
            model=self.config.vision_model_name,
            prompt=prompt,
            image_paths=image_paths,
            temperature=0.2,
        )
        summary = str(payload.get("summary", "")).strip()
        implications = str(payload.get("implications", "")).strip()
        return f"{summary}\n\nImplications: {implications}".strip()

    def generate_image(self, prompt: str, output_path: Path) -> Path:
        if not self.supports_image_generation() or not self.config.ai_image_model:
            raise RuntimeError("Image generation is not available for the configured provider or model.")
        return self.provider.generate_image(self.config.ai_image_model, prompt, output_path)

    def generate_original_posts(self, topic: str, signals: list[dict[str, Any]], count: int) -> list[DraftPayload]:
        prompt = f"""
You are drafting original X posts for the user.

Voice packet:
{self.style_packet}

Requested topic:
{topic or "Use the strongest current signals"}

Recent signals:
{json.dumps(signals, indent=2)}

Rules:
- Use the current signal set, not generic timeless advice.
- Focus on AI, builder workflow, product implications, or what the market is missing.
- Return exactly {count} options.
- Mix formats and endings naturally across options.
- Return JSON array only with keys: text, rationale, image_prompt, image_reason.
"""
        raw = self.provider.generate_json(self.config.ai_polish_model, prompt, temperature=0.85)
        results: list[DraftPayload] = []
        for item in raw:
            results.append(
                DraftPayload(
                    draft_type="original",
                    text=str(item["text"]).strip(),
                    rationale=str(item.get("rationale", "")).strip(),
                    image_prompt=(str(item["image_prompt"]).strip() if item.get("image_prompt") else None),
                    image_reason=(str(item["image_reason"]).strip() if item.get("image_reason") else None),
                )
            )
        return results

    def propose_voice_update(
        self,
        whoami_text: str,
        voice_text: str,
        humanizer_text: str,
        learning_events: list[dict[str, Any]],
    ) -> dict[str, str]:
        prompt = f"""
You are reviewing a user's local X drafting behavior and proposing an improved Voice.md file.

WhoAmI.md:
{whoami_text}

Current Voice.md:
{voice_text}

Humanizer.md:
{humanizer_text}

Recent learning events:
{json.dumps(learning_events, indent=2)}

Task:
- Study what the user actually approved, rejected, and edited.
- Use edits as the highest-signal feedback.
- Update the Voice.md guidance so future drafts better match the user's real behavior.
- Preserve the `## Active Guardrails` section from the current Voice.md exactly.
- Revise only the sections above that guardrails block.

Output:
- Return JSON only.
- Keys: summary_text, proposed_voice_md
"""
        payload = self.provider.generate_json(self.config.ai_polish_model, prompt, temperature=0.35)
        return {
            "summary_text": str(payload.get("summary_text", "")).strip(),
            "proposed_voice_md": str(payload.get("proposed_voice_md", "")).strip(),
        }

    def propose_archive_voice_update(
        self,
        whoami_text: str,
        voice_text: str,
        humanizer_text: str,
        archive_summary_text: str,
        archive_examples: list[dict[str, Any]],
    ) -> dict[str, str]:
        prompt = f"""
You are turning an imported X archive into a better Voice.md file for the user.

WhoAmI.md:
{whoami_text}

Current Voice.md:
{voice_text}

Humanizer.md:
{humanizer_text}

Archive-derived voice summary:
{archive_summary_text}

Representative archive posts:
{json.dumps(archive_examples, indent=2)}

Task:
- Use the archive as the high-signal record of how the user actually writes on X.
- Propose a stronger Voice.md that captures the user's real habits, tone, openings, patterns, and anti-patterns.
- Do not turn WhoAmI facts into voice rules.
- Do not rewrite Humanizer.md.
- Preserve the `## Active Guardrails` section from the current Voice.md exactly.
- Revise only the sections above that guardrails block.
- Be concrete and avoid generic “thought leader” language.

Output:
- Return JSON only.
- Keys: summary_text, proposed_voice_md
"""
        payload = self.provider.generate_json(self.config.ai_polish_model, prompt, temperature=0.25)
        return {
            "summary_text": str(payload.get("summary_text", "")).strip(),
            "proposed_voice_md": str(payload.get("proposed_voice_md", "")).strip(),
        }
