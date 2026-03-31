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
- Keep `profiles/default/Voice.md` as the primary long-form voice source.
- Keep `profiles/default/Humanizer.md` as the last-pass short-form constraint layer.
- Fill and revise the sections above this block. Do not rewrite this block during normal setup.
```

## What To Paste With The Prompt
- your completed `WhoAmI.Questionnaire.md`
- your completed `Voice.Questionnaire.md`
- optional sample tweets or replies if you want the result to sound more precise
