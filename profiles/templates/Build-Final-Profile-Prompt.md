# Build Final Profile Prompt

Use this after you have answered the questionnaires.

## Prompt
Copy this into ChatGPT, Gemini, or another agent along with your completed questionnaire answers:

```text
I am building the final profile files for a local X drafting dashboard.

You will receive:
- a completed WhoAmI Questionnaire
- a completed Voice Questionnaire

Task:
1. Turn those answers into two final Markdown files:
   - profiles/default/WhoAmI.md
   - profiles/default/Voice.md
2. Make them concise, specific, and useful for a drafting system.
3. Preserve my actual tone and constraints.
4. Do not add made-up biography details.
5. Do not write marketing copy.
6. Keep examples and rules that will help a drafting assistant sound like me.
7. Keep the `## Active Guardrails` section at the bottom of each file.
8. Preserve the guardrail bullets exactly as written below.

WhoAmI requirements:
- Keep it factual.
- Keep it anchored in identity, products, audience, perspective, and constraints.
- Do not turn it into a voice memo.

Voice.md requirements:
- Make it operational, not descriptive fluff.
- Use short paragraphs and bullet lists.
- Optimize for substance, specificity, context fidelity, and strong in-feed writing.
- The goal is not to sound profound on every tweet.
- The goal is to post like a smart technical founder who actually has something to say.
- Explicitly block repeated worldview injection, generic "thought leader" phrasing, and unrelated product-thesis pivots.
- Treat favorite themes as optional lenses, not universal answers, unless the source clearly earns them.
- Keep the file source-faithful so replies stay inside the topic of the source tweet before reaching for a broader thesis.
- Use this exact section order for `profiles/default/Voice.md`:
  - `# VOICE`
  - intro paragraph matching the operational purpose above
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

Output format:
- First return the full contents for `profiles/default/WhoAmI.md`
- Then return the full contents for `profiles/default/Voice.md`

Required guardrails for `profiles/default/WhoAmI.md`:

## Active Guardrails

- Keep this file factual. It should anchor identity, products, audience, and perspective.
- Do not turn this file into a tone guide. Tone belongs primarily in `profiles/default/Voice.md`.
- Fill and revise the sections above this block. Do not rewrite this block during normal setup.

Required guardrails for `profiles/default/Voice.md`:

## Active Guardrails

- Keep `profiles/default/WhoAmI.md` for factual identity, product context, and audience framing.
- Keep `profiles/default/Voice.md` as the primary short-form voice source.
- Keep `profiles/default/Humanizer.md` as the last-pass short-form constraint layer.
```

## What To Paste With The Prompt
- your completed `WhoAmI.Questionnaire.md`
- your completed `Voice.Questionnaire.md`
- optional sample tweets or replies if you want the result to sound more precise
