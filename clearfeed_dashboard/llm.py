from __future__ import annotations

import json
from datetime import datetime, timezone
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

Example 5
Target tweet:
"Qwen 3.6 Plus is officially the first model on OpenRouter to break 1 Trillion tokens processed in a single day."

Weak reply:
"1.4T tokens in a day is a massive milestone. The reason Qwen is pulling these numbers is its edge in code generation and structured reasoning. It handles complex agentic loops and multi-step instruction following with way less laziness than other frontier models. Congrats to the Qwen team for hitting 1T."

Stronger reply:
"1.4T tokens in a day is a massive milestone. Also kind of tracks with what I've been hearing around Qwen lately. From the people I've talked to, it seems especially solid for code and more structured reasoning work, which probably helps a lot for agentic workflows too. Congrats to the Qwen team for hitting 1T."

Why the stronger reply works:
- It frames thin-evidence claims as personal observation instead of fact.
- It sounds like an in-feed reaction, not a benchmark memo.
- It keeps the point to one or two grounded claims instead of stacking a capability list.
""".strip()

VOICE_MD_BLUEPRINT = """
Return a complete Markdown file for `Voice.md` using this structure and intent:

# VOICE

This is the active voice profile for short-form drafting.
Use it with `WhoAmI.md` for factual grounding and `Humanizer.md` for the final pass.

This version is optimized for substance, specificity, and strong in-feed writing.
The goal is not to sound profound on every tweet.
The goal is to post like a smart technical founder who actually has something to say.

Required sections, in order:
- `## Core Objective`
- `## Identity To Draw From`
- `## Substance Standard`
- `## Context Fidelity Rule`
- `## What Strong Posts Usually Do`
- `## Topic Priorities`
- `## House Style`
- `## Platform-Aware Rules`
- `## Writing Moves To Use More`
- `## Writing Moves To Use Less`
- `## Specificity Rules`
- `## Continuity Rule`
- `## Preferred Tone`
- `## Pacing And Rhythm`
- `## Openings That Fit`
- `## Endings That Fit`
- `## Lexicon`
- `## Reply Rules`
- `## Quality Bar`
- `## Active Guardrails`

Writing requirements:
- Make the file operational, not descriptive fluff.
- Use short paragraphs and bullet lists.
- Encode anti-patterns explicitly, especially repeated worldview injection or topic drift.
- Treat recurring house themes as optional lenses, not default answers, unless the evidence clearly says otherwise.
- Keep the voice source-faithful: the file should help the model stay inside the topic of the source tweet before reaching for a broader thesis.
- Prefer specificity, mechanisms, product or technical nouns, and practical implications over generic "thought leader" language.
- Do not turn WhoAmI facts into automatic talking points.
- Do not stuff the file with archive statistics or corpus-analysis language.
- Preserve the existing `## Active Guardrails` block exactly.
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
- Prefer fresh posts with real implications for builders, AI products, APIs, launches, devtools, startup/product operators, or contrarian technical takes.
- Relevance outranks raw velocity. Strong niche fit to tech/AI/builder/product topics should beat generic viral discourse.
- If the source is the home timeline, prioritize tweets that feel early but already show traction: velocity, healthy engagement, second-degree social proof, or obvious launch energy, but only when they still fit the niche.
- Reward strong breakout posts from accounts outside the curated lists when there is still a concrete reply angle and the post is clearly adjacent to the niche.
- Prefer posts where a reply or quote reply can add a concrete angle.
- Treat politics, geopolitics, or culture-war discourse as opportunistic only. Usually mark those as watch unless they are directly tied to tech, AI, policy affecting builders, or the upside is exceptional.
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
- sound like a benchmark report, product brief, or vendor analyst memo
- rely on vague agreement like "great point", "this is important", or "facts"
- inject the user's product or identity when it is not clearly relevant
- could be pasted under many unrelated tweets with only minor edits
- sound like a polished mini-essay instead of an in-feed reply

### Hard drafting rules
- Sound like the user, not a corporate account.
- The default is to sound like a sharp builder on X, not to self-introduce.
- No em dashes.
- Treat the voice packet as behavioral guidance, not as a bank of reusable slogans, closers, or worldview fragments.
- Stay tightly anchored to the exact target post.
- Use at least one concrete detail from the target tweet, thread, image, linked page, or user guidance when possible.
- If Expanded context from linked page is present, treat it as primary grounding material.
- If Live web research is available, use it to understand the broader situation around the linked page, launch, claim, or company before drafting.
- If User drafting guidance is present, treat it as the steering brief unless it would make the reply inaccurate.
- If the source does not prove why a model, company, or product is good, do not present the reason as settled fact.
- When the point is based on personal experience, industry chatter, or a cautious read of the situation, mark it that way in the wording.
- Prefer phrasing like "from what I've seen", "from what I've heard", "in my experience", "seems like", or "probably" over unsupported confident claims.
- For model or company news, keep inferred strengths to one or two grounded points instead of dumping a polished capability list.
- Do not import recurring house themes unless the target tweet, linked context, or user guidance clearly earns them.
- If the post is thin, keep the reply light and specific instead of forcing a big thesis.
- Prefer one sharp point over multiple weak points.
- Prefer source-specific nouns, mechanisms, and tradeoffs over abstract manifesto language.
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

    def build_originals_research_brief(self, topic: str, signals: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.supports_web_search():
            return {}
        prompt = f"""
You are researching live public-web context before drafting original X posts.

Current UTC timestamp:
{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

Requested topic:
{topic or "Use the strongest current AI, product, and builder signals"}

Recent local signal pool:
{json.dumps(signals, indent=2)}

Task:
- Use live web search to identify the most relevant current discussions, launches, claims, product shifts, or debates.
- Prioritize topics that are active now, not generic evergreen talking points.
- Bias toward AI, product, distribution, developer workflow, infra, and what operators or builders should notice.
- Focus on angles that could lead to a strong original X post.
- Keep the brief concise and practical.

Return JSON only with keys:
- summary
- themes: array of objects with keys theme, why_now, evidence
- opportunities: array of short strings
- avoid: array of short strings
"""
        return self.provider.generate_json(
            self.config.ai_text_model,
            prompt,
            temperature=0.35,
            use_web_search=True,
        )

    def suggest_original_post_topics(
        self,
        topic_hint: str,
        signals: list[dict[str, Any]],
        recent_original_drafts: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, str]]:
        research_brief: dict[str, Any] = {}
        if self.supports_web_search():
            try:
                research_brief = self.build_originals_research_brief(topic_hint, signals)
            except Exception:
                research_brief = {}
        prompt = f"""
You are helping the user decide what timely original X posts are worth writing.

Current UTC timestamp:
{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

Voice packet:
{self.style_packet}

Optional topic hint from the user:
{topic_hint or "None"}

Recent local signals:
{json.dumps(signals, indent=2)}

Recent original posts to avoid repeating:
{json.dumps(recent_original_drafts or [], indent=2)}

Live research brief:
{json.dumps(research_brief, indent=2) if research_brief else "Not available"}

Task:
- Suggest exactly {limit} timely topics the user could post about right now.
- Base them on current news, discourse, launches, arguments, or trend shifts, not evergreen filler.
- Favor ideas relevant to AI, builders, products, infra, workflow, and market implications.
- Each idea should be distinct.
- Give the user a clear angle they could take, not just a topic headline.
- Make each topic strong enough to support one substantial standalone post, not a throwaway reaction.
- Favor angles where the user can add something new to the conversation: a mechanism, tradeoff, market implication, operator takeaway, or informed disagreement.
- Make the angle specific enough that a user can decide "yes, I want to post on this" or "no".

Return JSON array only with keys:
- title
- why_now
- suggested_angle
- prompt_seed
"""
        raw = self.provider.generate_json(
            self.config.ai_text_model,
            prompt,
            temperature=0.45,
            use_web_search=self.supports_web_search(),
        )
        return [
            {
                "title": str(item.get("title", "")).strip(),
                "why_now": str(item.get("why_now", "")).strip(),
                "suggested_angle": str(item.get("suggested_angle", "")).strip(),
                "prompt_seed": str(item.get("prompt_seed", "")).strip(),
            }
            for item in raw
        ]

    def generate_original_posts(
        self,
        topic: str,
        signals: list[dict[str, Any]],
        count: int,
        recent_original_drafts: list[str] | None = None,
    ) -> list[DraftPayload]:
        research_brief: dict[str, Any] = {}
        if self.supports_web_search():
            try:
                research_brief = self.build_originals_research_brief(topic, signals)
            except Exception:
                research_brief = {}
        prompt = f"""
You are drafting original X posts for the user.

Current UTC timestamp:
{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

Voice packet:
{self.style_packet}

Requested topic:
{topic or "Use the strongest current signals"}

Recent signals:
{json.dumps(signals, indent=2)}

Recent original posts to avoid repeating:
{json.dumps(recent_original_drafts or [], indent=2)}

Live research brief:
{json.dumps(research_brief, indent=2) if research_brief else "Not available"}

Rules:
- Use the voice packet as a constraint system, not as a phrase bank.
- Use the current signal set, not generic timeless advice.
- If the live research brief is available, use it to ground the drafts in what is happening right now.
- If live web research is available for this provider, use it to verify the current situation before drafting instead of relying only on the summary brief.
- Focus on AI, builder workflow, product implications, market structure, or what the current discourse is missing.
- Return exactly {count} options.
- Treat these as longer-form X post drafts, not short 280-character tweets.
- Aim for roughly 500 to 900 characters when the topic can support it.
- Do not go below 500 characters unless the topic would clearly become padded, repetitive, or weaker by forcing more length.
- The post should read like a researched breakdown: lead with the live topic, unpack the mechanism or evidence, then land on a useful implication or non-obvious takeaway.
- Every option should do one valuable thing well: teach, sharpen, reframe, warn, predict, or name a practical implication.
- Add something new to the conversation instead of restating the obvious consensus take.
- Use specific nouns, product examples, company names, or market details when they materially improve the post.
- Include at least one concrete detail that clearly came from current signals or research.
- Sound human and in-feed, not like a press release, consultant memo, or AI thread template.
- Two short paragraphs are fine when they improve readability.
- No em dashes.
- Avoid generic "AI is changing everything" language, vague hype, or empty engagement bait.
- Avoid repeating the recent original posts list.
- Suggest an image only when a simple explainer visual would materially help the post.
- Return JSON array only with keys: text, rationale, image_prompt, image_reason.
"""
        raw = self.provider.generate_json(
            self.config.ai_originals_model,
            prompt,
            temperature=0.8,
            use_web_search=self.supports_web_search(),
        )
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
- Approved and edited drafts matter more than the current file when they disagree.
- If the learning events show recurring drift, write direct anti-pattern rules that block that drift.
- The file must help future drafts stay specific to the source topic instead of forcing the same worldview onto unrelated tweets.
- Preserve the `## Active Guardrails` section from the current Voice.md exactly.
- Revise only the sections above that guardrails block.
- Do not produce a generic tone memo, archive report, or list of favorite words.
- Return the full Voice.md in the blueprint below.

Blueprint:
{VOICE_MD_BLUEPRINT}

Output:
- Return JSON only.
- Keys: summary_text, proposed_voice_md
"""
        payload = self.provider.generate_json(self.config.ai_voice_review_model, prompt, temperature=0.35)
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
- The file must make source fidelity explicit so future replies do not drag unrelated posts back to one favorite thesis.
- If archive themes like memory, continuity, identity, or similar concepts appear, treat them as scoped lenses rather than universal closers unless the archive overwhelmingly proves otherwise.
- Preserve the `## Active Guardrails` section from the current Voice.md exactly.
- Revise only the sections above that guardrails block.
- Be concrete and avoid generic "thought leader" language.
- Return the full Voice.md in the blueprint below.

Blueprint:
{VOICE_MD_BLUEPRINT}

Output:
- Return JSON only.
- Keys: summary_text, proposed_voice_md
"""
        payload = self.provider.generate_json(self.config.ai_archive_voice_model, prompt, temperature=0.25)
        return {
            "summary_text": str(payload.get("summary_text", "")).strip(),
            "proposed_voice_md": str(payload.get("proposed_voice_md", "")).strip(),
        }
