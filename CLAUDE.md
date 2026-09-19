# prompt_optimization — promptcompression.ai

Downstream of **bpto** (https://github.com/sign-of-fourier/bpto), consumed strictly as a pinned git dependency.
Two deliverables live here:

- `compression/` — the static site served at `promptcompression.ai` (nginx web root). Plain HTML/CSS, no build
  step. `python gen-llms.py` (run from inside `compression/`) regenerates `llms-full.txt` and sitemap `lastmod`
  after editing any page. `skills/prompt-compression/SKILL.md` is the installable Claude Code skill the site advertises.
- `studio/` — its own git repo: FastAPI backend (`app/`) + React/React Flow canvas (`web/`), served at
  `promptcompression.ai/studio/`. See `studio/README.md` for layout, run-locally steps and invariants.

`.env` files (root and `studio/`) hold API keys and are gitignored; never print or commit them.

## The bpto boundary (read this first)

This project knows bpto **only through its GitHub repository** at the commit pinned in `studio/pyproject.toml`
(`bpto @ git+https://github.com/sign-of-fourier/bpto@<sha>`). What that gives you:

- the public API in `bpto/` (`Task`, `Program`, `ModelClient` + clients, `evaluate`, `tree.apply`, ops, selectors,
  `BOSelector`, `gepa`, `CompletionCache`, `Budget`, the executor), documented in the repo `README.md` and
  `BO_ACQUISITION.md`;
- `tasks/`, `examples/`, `tests/` as reference usage;
- `experiments/` — committed findings (NOTES.md, reports, plots) that the site's Findings page quotes.

**Do not** read, reference, or depend on anything outside that repository's tracked files. A local checkout of
bpto exists elsewhere on this machine: never cd, ls, cat, grep, glob or read under it, never follow `pip show bpto` /
editable-install paths to it, and never record its location anywhere (memory, this file, comments, config, deploy
files). Its working notes, run artifacts, caches and credentials are *not* part of bpto's contract and must not
leak into this project — not as code paths, not as comments, not as facts quoted on the site. If you need to know
how bpto behaves, read the installed package's public modules (`python -c "import bpto, inspect; ..."`) or fetch
the file from the GitHub URL at the pinned sha, and cite the README or `experiments/` there. If bpto lacks something the studio needs, the fix goes upstream in bpto (as a normal PR
against the GitHub repo), then bump the pin here.

To bump the pin: change the sha in `studio/pyproject.toml`, `pip install -e studio[dev]`, run the studio tests.

## Commands

```bash
# studio
cd studio && pip install -e .[dev] && python -m pytest -q        # offline: every test uses bpto's MockClient
(cd studio/web && npm install && npm run build)                   # -> web/dist, served by nginx
cd studio && STUDIO_MOCK=1 STUDIO_INSECURE_COOKIE=1 uvicorn app.main:app --port 8100   # http://localhost:8100/studio/
# site
cd compression && python gen-llms.py                              # after editing any page
```

Deploy: `studio/deploy/nginx-studio.conf` (location blocks + web root) and `studio/deploy/studio.service`
(systemd unit running uvicorn on 127.0.0.1:8100). The studio's code, sqlite db and keys live outside the web root.

## Conventions

- Tests never hit the network. `STUDIO_MOCK=1` routes every model call to the schema-generic mock in `app/mock.py`.
- Live runs cost money: every run carries its own `CompletionCache`, `tree.json` checkpoint, `events.jsonl` and a
  `Budget(max_usd)`; the pilot (root twice + one rewrite) runs before any spend is committed. Don't start a live run
  yourself without stating the projected cost.
- Evaluation calls pin `temperature=0.0`; reflection has its own temperature (`spec.optimizer.reflect_temperature`).
- Executor metrics (`steps`, `tokens_per_module.*`, `parse_fail.*`) join the metric vector; the objective weighs
  them — nothing is scalarized inside a scorer.
- The canvas spec (`app/models.py`, `ProjectSpec`) is plain JSON and the contract between `web/` and `app/`; change
  both sides together, and `app/compile.py` is the only place that turns a spec into bpto objects.
- Site copy that states a number or finding must trace to a bpto `experiments/*/NOTES.md` on GitHub.
