from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import AppConfig
from .types import CandidateDecision, DraftPayload
from .vertex import VertexClient


class DraftingEngine:
    def __init__(self, config: AppConfig, style_packet: str):
        self.config = config
        self.style_packet = style_packet
        self.vertex = VertexClient(config)

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
        raw = self.vertex.generate_json(self.config.gemini_text_model, prompt, temperature=0.3)
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
        use_web_search = bool(candidate.get("linked_url"))
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
{"Available via Vertex web grounding for this request. Use it when it materially improves specificity or accuracy." if use_web_search else "Not enabled for this request."}

Rules:
- Sound like the user, not a corporate account.
- Use the profile packet for taste and background, not as copy to repeat.
- Do not inject identity details, job titles, or background unless they are genuinely relevant to the point.
- The default is to sound like a sharp builder on X, not to self-introduce.
- No em dashes.
- Add one real thing: a reaction, question, mechanism, disagreement, concrete observation, or implication. Do not force the most thesis-shaped option if a simpler response fits better.
- Stay tightly anchored to the target post. The reply should obviously belong under this specific tweet, not 50 adjacent AI tweets.
- Use at least one concrete detail from the target tweet or surrounding context when possible: a product name, feature, claim, tradeoff, workflow, or quoted phrase.
- If Expanded context from linked page is present, treat it as primary grounding material and use it when drafting. Do not ignore it and fall back to a generic AI take.
- If Live web research is available, use it to understand the broader situation around the linked page, launch, claim, or company before drafting. Do not pretend to have researched anything you did not actually check.
- If User drafting guidance is present, treat it as the steering brief for the draft unless it would make the reply inaccurate or detached from the source tweet.
- If the post is thin, do not force a grand thesis. Prefer a sharp concrete observation, disagreement, or question over a generic worldview riff.
- Do not pivot into favorite house themes like continuity, memory, trust, orchestration, or "the real story" unless the target tweet genuinely supports that move.
- Before finalizing, check whether the draft could be pasted under a different AI tweet with almost no changes. If yes, rewrite it to be more specific.
- Favor language that sounds like a person reacting in-feed, not a polished mini-essay. Some texture is better than "perfect" AI smoothness.
- Do not flatter the original poster.
- Keep it concise enough for X.
- Use the shape that fits X. One paragraph is normal. A line break is fine if it helps. Do not force either dense blocks or stacked one-liners.
- Make the take more thought-provoking by naming the mechanism, tradeoff, market implication, or second-order effect, not by using dramatic filler.
- Avoid generic AI-post templates, vague contrarian openings, and lines that could fit dozens of adjacent AI tweets with no edits.
- Do not make every draft feel clean, symmetrical, and over-shaped. A little roughness is better than ghostwriter smoothness.
- Use short standalone lines sparingly. They should add force, not become the whole rhythm.
- Prefer concrete nouns and product details over abstract language that only gestures at depth.
- If a dramatic reframe like "wrong question" or "the real story" is not clearly earned by the source, do not use it.
- For reply: respond directly to the post.
- For quote_reply: create a standalone sharp take that still references the post.
- If the target tweet is replying to or quoting something, use that surrounding context to avoid missing the actual argument.
- Suggest an image only if a simple diagram or technical explainer visual would materially help.
- Internally draft 3 candidate replies, pressure-test them for specificity and voice, then return only the strongest final option.
- A strong final reply should pass this test:
  1. specific to the source
  2. naturally in voice
  3. says one real thing
  4. does not sound like AI content sludge
- Return JSON only with keys: text, rationale, image_prompt, image_reason.
"""
        payload = self.vertex.generate_json(
            self.config.gemini_text_model,
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
        if not image_paths:
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
        payload = self.vertex.generate_json_with_images(
            model=self.config.gemini_text_model,
            prompt=prompt,
            image_paths=image_paths,
            temperature=0.2,
        )
        summary = str(payload.get("summary", "")).strip()
        implications = str(payload.get("implications", "")).strip()
        return f"{summary}\n\nImplications: {implications}".strip()

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
- Mix formats and endings naturally across options. Do not let every option use the same setup, cadence, or punchline shape.
- Return JSON array only with keys: text, rationale, image_prompt, image_reason.
"""
        raw = self.vertex.generate_json(self.config.gemini_polish_model, prompt, temperature=0.85)
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

Inputs:
- WhoAmI.md
- current Voice.md
- Humanizer.md
- recent learning events from the dashboard

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
- Keep the file concise and practical.
- Preserve the user's tone rather than flattening it.
- Do not rewrite Humanizer.md.
- Do not add made-up biography details.
- Preserve the `## Active Guardrails` section from the current Voice.md exactly.
- Revise only the sections above that guardrails block.

Output:
- Return JSON only.
- Keys:
  - summary_text: short explanation of the most important voice changes you are proposing
  - proposed_voice_md: the full proposed contents of Voice.md
"""
        payload = self.vertex.generate_json(self.config.gemini_polish_model, prompt, temperature=0.35)
        return {
            "summary_text": str(payload.get("summary_text", "")).strip(),
            "proposed_voice_md": str(payload.get("proposed_voice_md", "")).strip(),
        }
