# Voice

This is the final voice file the app reads.

You can create it in one of two ways:
- answer the questionnaires in `profiles/templates/` and then use the build prompt there to generate this file
- give the AI-assist templates in `profiles/templates/` to ChatGPT, Gemini, or another agent and have it draft this file for you based on what it knows plus your corrections

Edit the sections above `## Active Guardrails`.
Do not remove or rewrite the `## Active Guardrails` section at the bottom unless you are intentionally changing the system's voice-source hierarchy.

## Voice Snapshot
- Tone:
- Pacing:
- What makes my posts feel human:
- What makes my posts feel like me:

## Strong Examples
- Paste or summarize 8-15 good examples of your own posts or replies.
- Explain what works in them.

## Weak Examples
- Paste or summarize 3-5 examples that do not sound like you.
- Explain what fails in them.

## Writing Rules
- Things to preserve:
- Things to avoid:
- What makes a reply specific:
- Endings I like:
- Endings I dislike:

## Humanizer Notes
- Ways I naturally soften or roughen language:
- Casual markers I use:
- Emoji behavior:
- Rhythm or formatting patterns:

## Active Guardrails

- Keep `profiles/default/WhoAmI.md` for factual identity, product context, and audience framing.
- Keep `profiles/default/Voice.md` as the primary long-form voice source.
- Keep `profiles/default/Humanizer.md` as the last-pass short-form constraint layer.
- Fill and revise the sections above this block. Do not rewrite this block during normal setup.
