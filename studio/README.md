# studio

**Impromptune**: the point-and-click front end for [bpto](https://github.com/sign-of-fourier/bpto). Draw a program
(prompt steps wired into a graph, orchestrating steps that choose the next step), attach a labelled dataset, validate
it, run GEPA or BO in the expand seat, watch the tree. Served at `impromptune.com/app/`; the marketing site and docs at that host's root are `../impromptune/`. The name is configuration
(`STUDIO_NAME`, `STUDIO_PUBLIC_URL` in `.env`; `VITE_STUDIO_NAME` at build time) because it may change.

```
app/          FastAPI backend (routes are unprefixed; nginx mounts them at /api/, dev mode also accepts /studio/api/)
  models.py   the canvas spec (ProjectSpec: modules, edges, evaluate, optimizer) - plain JSON
  compile.py  spec -> bpto Program / Task / run() schedule (GEPA or BO parents, minibatch gate, LLM critic feedback)
  scorers.py  point-and-click scorers (exact match, contains, token F1, regex, JSON field, numeric, LLM judge ± reference)
  clients.py  RoutingClient: one bpto ModelClient per run that dispatches on config.model (Bedrock / Anthropic / OpenAI)
  validation.py  graph, mapping, labels, scorer checks; pilot (root twice + one rewrite); cost projection
  brand.py    the studio's name and public URL (env-driven)
  datasets.py runs.py auth.py db.py main.py
web/          React + React Flow canvas (vite); `VITE_STUDIO_BASE=/app/ VITE_API_BASE=/api npm run build` -> web/dist, served at impromptune.com/app/
              (default base /studio/ is what `uvicorn app.main:app` serves in dev). Brand strings live in src/brand.js.
  bundles.py  project bundles: one JSON = spec + dataset + blurb; the examples library, export and import share it
examples/     the library: checked-in bundles (`GET /examples`, `POST /examples/{slug}/clone`); rows come from sample/
sample/       the tutorial dataset
tests/        offline (bpto MockClient), incl. an end-to-end API flow
```

To add a library entry: build the project in the studio, Export it, drop the file in `examples/` with a `blurb` and an
`order`, and replace inline `rows` with `"sample": "<file in sample/>"` if it uses a checked-in dataset. Runs are never
bundled; "the optimized prompts" is a spec whose templates are them (`GET /projects/{pid}/bundle?run_id=` does that).
Inline rows are capped at 2 MB; larger datasets export without rows until datasets move to S3.

## Run locally

```bash
pip install -e .[dev]                 # installs bpto from git (pinned commit in pyproject)
python -m pytest -q
(cd web && npm install && npm run build)       # dev build: base /studio/
STUDIO_MOCK=1 STUDIO_INSECURE_COOKIE=1 uvicorn app.main:app --port 8100   # open http://localhost:8100/studio/
```

`STUDIO_MOCK=1` routes every model call to a schema-generic mock (no keys, no spend) - plumbing only.
Live runs read keys from `.env` (`AWS_BEARER_TOKEN_BEDROCK`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`).

## Invariants carried over from bpto

- Evaluation calls pin `temperature=0`; reflection has its own temperature.
- The survive seat (minibatch gate) is shown (accepted / proposed) but not configurable beyond minibatch size (floor 3).
- Executor metrics (`steps`, `tokens_per_module.*`, `parse_fail.*`) join the metric vector; the objective weighs them.
- Every run has its own `CompletionCache`, `tree.json` checkpoint, `events.jsonl`, and a `Budget(max_usd)`.
- The optimizer's goal (`optimizer.goal`) picks the reflection prompt: `accuracy` is bpto's REFLECT_PROMPT (add rules that
  fix the shown failures; it only ever lengthens a prompt), `compress` is the studio's shorten-and-keep-what-works prompt
  modelled on bpto `tasks/compression`, with correct rows shown as successes and the template token count in the feedback.
  `template_tokens` (templates only, summed over every step) is the metric to penalize; a weight alone changes nothing.
- The run's headline "best" is the top score among nodes evaluated on the whole training set; children scored only on the
  check batch are starred in the tree and never the headline.
- The pilot re-evaluates the root with the cache bypassed to measure the non-determinism band, and evaluates one
  random rewrite so a prompt that is flat under rewrites is caught before spend.
