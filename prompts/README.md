# Prompt overrides

Optional. The service loads this directory at startup (`REVIEW_PROMPT_DIR`, default
`prompts`). Missing files fall back to the built-in prompts.

## Global override

`security.md`, `pattern.md`, `general.md` — one per agent.

## Per-repository override

`repos/<owner>__<repo>/security.md` (double underscore between owner and repo),
for example `repos/acme__api/security.md`.

## Rules

- The template must keep the required placeholders:
  - `general`: `{title}`, `{diff}`, `{task_section}`, `{context_section}`, `{instructions}`
  - `security` / `pattern`: `{title}`, `{diff}`, `{task_section}`, `{context_section}`
- Literal braces must be doubled (`{{` / `}}`), same as `str.format`.
- Keep the untrusted-data delimiters (`<untrusted_diff>...</untrusted_diff>`) and the
  instruction to ignore embedded instructions.
- Invalid files are skipped with a warning; use `load_directory(..., strict=True)` in
  code if you want startup to fail instead.
