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

Output format:
- First return the full contents for `profiles/default/WhoAmI.md`
- Then return the full contents for `profiles/default/Voice.md`
```

## What To Paste With The Prompt
- your completed `WhoAmI.Questionnaire.md`
- your completed `Voice.Questionnaire.md`
- optional sample tweets or replies if you want the result to sound more precise
