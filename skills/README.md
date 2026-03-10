# Skills Library

This folder contains reusable skill runbooks for the bot. A skill is a small, focused playbook that can be referenced in prompts or used as guidance for consistent execution.

## Recommended Structure
- `skills/<skill-name>/SKILL.md` — the main skill definition
- `skills/<skill-name>/inputs.md` — optional input schema or examples
- `skills/<skill-name>/outputs.md` — optional output format guidance
- `skills/<skill-name>/notes.md` — optional safety notes or constraints

## Conventions
- Use clear, actionable steps.
- Prefer deterministic commands and small, verifiable actions.
- Keep secrets out of files. Use `.env` or runtime env vars.
- If a skill can be destructive, call it out explicitly.

## Template
Use `skills/templates/SKILL.md` as a starting point.
