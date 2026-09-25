# Words we use, and words we do not

Settled 2026-09-25. The point is that the code, the UI and the documentation say the same thing, and that two of us
can argue about a feature without first establishing which feature.

Two collisions caused most of the confusion so far: **"step"** meant both a prompt and an external call, and
**"credential"** meant four different things. Both are fixed below.

## On the canvas

| Say | Never say | What it is |
|---|---|---|
| **prompt** | module, step | A node the optimizer rewrites. The program is made of prompts. `ProjectSpec.modules` keeps its name in code because that is bpto's word, but nothing user-facing says "module". |
| **router** | orchestrator, branch | A prompt with more than one outgoing edge: the model returns `next` and picks the path. A router is still a prompt and is still optimized. |
| **step** | tool, plugin, integration, node | A node that is **not** part of the program: an external call the optimizer never rewrites. Always "step", never bare "step" for a prompt. |
| **program** | pipeline, chain, graph | The prompts and edges together — what gets optimized. |
| **evaluate** | scorer block | The terminal block: scorers, objective, hold-out. |

The one-line version, which should appear in the docs verbatim: **a program is made of prompts; steps fetch things
from outside it.** Prompts change during a run. Steps do not.

## Authorisation

Four different things were all called "credentials". They are:

| Say | What it is | Where it lives | Whose |
|---|---|---|---|
| **house key** | our provider account, spent on behalf of paid tiers | `.env` | the platform's |
| **endpoint** | a user's own model provider account | `credentials` table, encrypted | the user's |
| **step key** | a secret the user pastes so a step can call a service | `step_credentials`, encrypted | the user's |
| **connection** | an OAuth grant: permission to act on the user's own account elsewhere | `connections`, encrypted | the user's |
| **app credentials** | client id and secret identifying *this studio* to a provider | `.env` | the platform's |
| **API key** | a key a caller sends *to us* to reach a served version | `api_keys`, sha256 only | the user's |

"Credential" on its own is banned in UI copy. Say which.

The rule that decides where a new secret goes: **a credential identifying the platform belongs in `.env`; a
credential belonging to a person belongs in a table, encrypted, scoped by `user_id`.** App credentials are the
platform's even when a founder created them in their own developer account.

A step's manifest declares how it signs in:

| `auth.kind` | UI offers | Use when |
|---|---|---|
| `key` | paste a step key | the provider issues keys, or it is an internal service |
| `oauth` | **Connect** | the step acts on the user's own account and we have app credentials registered |
| `either` | Connect if we have app credentials for that provider, otherwise paste a key | the honest default for a third-party service |

`bearer` is accepted as an old spelling of `key`; it named the transport rather than the thing.

## Running and shipping

| Say | Never say | What it is |
|---|---|---|
| **run** | job, experiment | One optimization: rounds of rewrite, gate, evaluate. |
| **baseline run** | eval-only, dry run | A run with `rounds = 0`: evaluates the prompts as written and stops. |
| **round** | iteration, generation | One pass of the search loop. |
| **gate** | filter | The minibatch check a rewrite must pass before earning a full evaluation. |
| **hold-out** | test set, validation set | Rows the search never sees. The number that counts. |
| **version** | deployment, release | An immutable published snapshot: prompts, models, steps, the score it earned. |
| **trace** | log, record | One served request and what it produced. |
| **outcome** | feedback, correction | What happened after the answer. A trace plus an outcome is a **label**. |
| **template** | example, sample, demo | A library entry you add to your projects. What you get is a **project**. |

## Data

| Say | Never say | What it is |
|---|---|---|
| **dataset** | training data, corpus | Rows with inputs and a label column. |
| **label** | answer, target, ground truth | The right answer for a row. (`answer` survives in code as bpto's field name.) |
| **freeze** | cache, snapshot, sync | Running a step over every row once and writing its answers onto the dataset as columns. Evaluation reads the frozen columns; production calls the step live. |
| **enrichment** | augmentation | What freezing produces: the step's columns on the dataset. |

## Things we deliberately do not have

Saying these implies a promise we have not made:

- **plugin / marketplace / registry** — steps are manifests we curate. Nobody else publishes.
- **agent** — nothing here decides its own goals or writes its own code.
- **fine-tuning / training** — we change prompts, never weights.
- **guaranteed / optimal / best** — the search reports a hold-out number with a standard error. It does not promise.
