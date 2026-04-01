from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import AppConfig
from .providers import AIProvider, build_provider
from .types import CandidateDecision, DraftPayload

REPLY_QUALITY_EXAMPLES = """
Few-shot examples:

Example 1
Target tweet:
"Most AI products fail because teams optimize the model before they fix the workflow."

Weak reply:
"Great point. Workflow really is everything."

Stronger reply:
"Yeah, a lot of teams still treat workflow debt like a polish issue. In practice it is usually the main failure mode: handoff friction, missing context, and vague next steps kill adoption before model quality does."

Why the stronger reply works:
- It adds a mechanism.
- It stays anchored to the claim.
- It says something the original tweet did not already say.

Example 2
Target tweet:
"Open source models are catching up faster than most people expected."

Weak reply:
"This space is moving so fast."

Stronger reply:
"The speed is real, but the more interesting shift is distribution. Once the capability gap narrows enough, deployment convenience and workflow fit start mattering more than leaderboard differences."

Why the stronger reply works:
- It sharpens the point instead of echoing it.
- It introduces a concrete implication.

Example 3
Target tweet:
"Most founders do not need more ideas. They need better judgment about what to ignore."

Weak reply:
"Facts. Focus is underrated."

Stronger reply:
"A lot of bad product decisions are really filtering failures. Teams keep weak opportunities alive too long, then call it strategy when execution gets diluted."

Why the stronger reply works:
- It reframes the idea with a sharper lens.
- It avoids empty agreement.

Example 4
Target tweet:
"If your onboarding needs a tutorial video, the product probably is not ready."

Weak reply:
"Totally agree."

Stronger reply:
"Usually true, though there is a useful exception: complex tools can still need orientation. The real red flag is when the video explains basic navigation instead of helping the user form a mental model."

Why the stronger reply works:
- It adds an edge case.
- It disagrees productively instead of performing agreement.
""".strip()


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

### Primary objective
Write a reply that improves the conversation instead of merely participating in it.
The final draft should add genuine value: sharper thinking, a concrete example, a mechanism, a useful disagreement, a non-obvious question, an edge case, or a practical implication.

### Voice packet
{self.style_packet}

### Context boundary for profile information
Use the profile packet to understand the user's taste, worldview, references, and writing instincts.
Do not treat the profile packet as default tweet content.
Do not inject the user's company, project, product, biography, role, or background unless:
- the target tweet makes that directly relevant, or
- User drafting guidance clearly asks for it.
If User drafting guidance mentions a project, company, or internal context from the profile packet, use the packet to resolve the reference correctly. Do not expand that into self-promotional copy unless the guidance explicitly calls for it.

### Target post
{json.dumps(candidate, indent=2)}

### Tweet thread / quote / reply context
{tweet_context or "None"}

### Tweet image context
{image_context or "None"}

### Expanded context from linked page
{article_context or "None"}

### User drafting guidance
{user_guidance or "None"}

### Live web research
{"Available for this request. Use it when it materially improves specificity or accuracy." if use_web_search else "Not enabled for this request."}

### What a strong reply should do
A strong reply should usually do one clear thing well:
- sharpen the original point
- add a concrete example
- explain the mechanism behind the claim
- name an edge case, caveat, or tradeoff
- ask a non-obvious question that advances the discussion
- offer a reasoned disagreement
- surface a practical implication for builders, operators, or users
- translate the point into plainer or more precise language

### Failure modes to avoid
Reject drafts that do any of the following:
- paraphrase the target tweet without adding anything new
- flatter the original poster
- sound like generic AI commentary
- rely on vague agreement like "great point", "this is important", or "facts"
- inject the user's product or identity when it is not clearly relevant
- could be pasted under many unrelated tweets with only minor edits
- sound like a polished mini-essay instead of an in-feed reply

### Hard drafting rules
- Sound like the user, not a corporate account.
- The default is to sound like a sharp builder on X, not to self-introduce.
- No em dashes.
- Stay tightly anchored to the exact target post.
- Use at least one concrete detail from the target tweet, thread, image, linked page, or user guidance when possible.
- If Expanded context from linked page is present, treat it as primary grounding material.
- If Live web research is available, use it to understand the broader situation around the linked page, launch, claim, or company before drafting.
- If User drafting guidance is present, treat it as the steering brief unless it would make the reply inaccurate.
- If the post is thin, keep the reply light and specific instead of forcing a big thesis.
- Prefer one sharp point over multiple weak points.
- Do not default to praise.
- Keep it concise enough for X, but do not compress so hard that the draft becomes generic.
- For a direct reply, stay close to the claim and push the conversation forward by one step.
- For a quote reply, you may zoom out one level and make the implication clearer, but still stay grounded in the original post.
- Suggest an image only if a simple diagram or technical explainer visual would materially help.

### Few-shot guidance
{REPLY_QUALITY_EXAMPLES}

### Internal selection process
Privately draft 3 candidates with different value modes.
For each candidate, pressure-test it on:
- specificity
- conversational value
- relevance to the exact post
- voice fit
- non-genericness
- whether it avoids unnecessary product or identity injection
Discard weak candidates.
Return only the strongest final option.

### Output format
Return JSON only with keys:
- text
- rationale
- image_prompt
- image_reason
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
