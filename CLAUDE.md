# prompt_optimization — quantecarlo.com · promptcompression.ai · impromptune.com

Downstream of **bpto** (https://github.com/sign-of-fourier/bpto), consumed strictly as a pinned git dependency.
Three hosts, one repo, one box:

- `quantecarlo/` — static site at `quantecarlo.com`: the optimizer and the company (prompt learning, GEPA vs BO,
  findings, About). Its nav links promptcompression.ai and Impromptune.
- `compression/` — static site at `promptcompression.ai`: content marketing for the compression objective
  (business case, worked studio example). Its nav links Impromptune. `skills/prompt-compression/SKILL.md` is the
  installable Claude Code skill. `style.css`, `favicon.svg`, `img/` are shared: `quantecarlo/` symlinks to them.
- `impromptune/` — the product site and documentation at `impromptune.com/`: landing page plus `docs/` (ten
  pages). Static like the other two, and generated into `llms-full.txt`/`sitemap.xml` by `gen-site.py`. `app/`
  inside it is a symlink to `studio/web/dist`, which is how the canvas is served at `/app/`.
- `studio/` — **Impromptune**, the app itself, at `impromptune.com/app/`: FastAPI backend (`app/`, proxied at
  `/api/`) + React/React Flow canvas (`web/`). See `studio/README.md` for layout, run-locally steps and
  invariants. The studio's name and public URL are placeholders (`STUDIO_NAME`, `STUDIO_PUBLIC_URL`,
  `VITE_STUDIO_NAME`): never inline them. The API stays at `/api/`, not under `/app/`, because the OAuth redirect
  URI registered with each provider names that path.

Marketing sites are plain HTML/CSS, no build step. `python gen-site.py all` (repo root) regenerates each of the three sites'
`llms-full.txt` and `sitemap.xml` after editing any page; `llms.txt` is hand-written per site. `deploy/` holds
the nginx config for all three hosts and the studio's systemd unit.

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
(cd studio/web && npm install && VITE_STUDIO_BASE=/app/ VITE_API_BASE=/api npm run build)  # -> web/dist, served at impromptune.com/app/ (default base is /studio/, used by dev mode)
cd studio && STUDIO_MOCK=1 STUDIO_INSECURE_COOKIE=1 uvicorn app.main:app --port 8100   # http://localhost:8100/studio/ (build without VITE_STUDIO_BASE for this)
# sites
python gen-site.py all                                            # after editing any page
```

Deploy: `deploy/nginx.conf` (three server blocks; old `/studio/` paths redirect to `impromptune.com/app/`),
`deploy/snippets-marketing.conf` (shared static-site snippet) and `deploy/studio.service` (uvicorn on
127.0.0.1:8100). The studio's code, sqlite db and keys live outside every web root.

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
- Site copy that states a number or finding must trace to a bpto `experiments/*/NOTES.md` on GitHub, or to a
  studio run whose screenshot is on the page. This binds `impromptune/` too: prefer linking quantecarlo.com's
  findings over restating a figure, and keep the division — quantecarlo owns the method and the evidence,
  promptcompression owns the compression business case, impromptune owns the product and its docs.
- The words in `GLOSSARY.md` are binding on UI copy and on `impromptune/`: a **prompt** is a node the optimizer
  rewrites, a **step** is an external call it never rewrites, and "credential" on its own is banned — say which.
  `studio/tests/test_hints.py` holds the in-app hint budget (eight) and asserts each one's doc anchor exists. The About page's company history numbers are Quante Carlo's own
  claims and are labelled as such there; don't repeat them on the technical pages.
