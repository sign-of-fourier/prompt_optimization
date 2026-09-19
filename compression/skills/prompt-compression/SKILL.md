---
name: prompt-compression
description: Compress an LLM prompt (fewer tokens, same accuracy) with the open-source bpto optimizer from promptcompression.ai. Use when the user wants a shorter/cheaper/faster prompt and can supply labeled examples, or asks to "compress", "shorten" or "optimize" a prompt against data.
---

# Prompt compression with bpto

Source and full instructions: https://promptcompression.ai/claude-code

## When to use
The user wants a shorter prompt without losing accuracy, and has (or can create) labeled
examples: inputs plus the correct output. This is template compression at optimization
time (a search over rewrites, scored on data) — not LLMLingua-style input compression.

## Collect from the user first
1. The current prompt, with `{placeholders}` for per-request inputs.
2. Labeled examples (50–100 is a good start; 30 is too few). Ask for production traces if they have them.
3. The production model and an API key (Anthropic, or any OpenAI-compatible endpoint).
4. An accuracy floor (default: within 0.05 of the original on a held-out set).

## Steps
1. Install:
   ```
   git clone https://github.com/sign-of-fourier/bpto && cd bpto && pip install -e .
   ```
   Python ≥ 3.12. `python -m pytest -q` runs offline tests.
2. Write `train.jsonl` and `heldout.jsonl` (about 2:1). One object per line:
   `{"inputs": {"text": "..."}, "answer": [...]}` — `inputs` keys match the prompt placeholders.
3. Smoke test with no API key: `python -m tasks.compression.run --mock --rounds 2`
4. Real run (name-extraction-shaped tasks work out of the box):
   ```
   ANTHROPIC_API_KEY=... python -m tasks.compression.run --data train.jsonl --rounds 4 --model claude-opus-5
   # or: --provider openai --base-url https://api.openai.com/v1 --model <model>
   # constrained: --constrained --start-tokens 80 --shrink 10
   # resume after crash: --resume
   ```
   Flags: `--n-random 3 --n-guided 2 --expand-k 3 --cheap-n 12 --token-weight 0.002 --concurrency 8 --out runs/compression`
5. Read `runs/compression/report.txt` (Pareto table + best prompt) and `pareto.png`.
   `tree.json` is the checkpoint; `cache.jsonl` the completion cache.
6. For a task that is not name extraction, write a task module: a Pydantic output `schema`,
   a `scorer(prompt, example, completion, ctx) -> dict` of metrics, and
   `Task(root=..., dataset=..., schema=..., scorer=combine(scorer, template_tokens(), token_count()),
   objective=LinearObjective(<metric>=1.0, template_tokens=-0.002), client=...)`.
   Then `tree = Tree(task)`; `await tree.apply(random(n=4))`;
   `await tree.apply(guided("make it as short as possible without losing precision", n=3))`;
   `await tree.apply(evaluate(), select=select.unevaluated)`; `tree.best()`, `tree.pareto(...)`.
   The QA example in the bpto README (llm_judge + token_count) is the template.

## Report to the user
Original tokens and held-out score; 2–3 points on the front (shortest feasible, safest, in between)
with their prompt text and held-out scores. Never report only the training score.

## Troubleshooting
- All children score like the root → the rewriter is paraphrasing; use a stronger model or a
  more forceful directive.
- Train high, held-out low → overfitting; more examples or a higher floor.
- Parse failures → check raw completions in `cache.jsonl`; small models sometimes echo the schema.
- Rate limits → lower `--concurrency`; use `--resume`.
