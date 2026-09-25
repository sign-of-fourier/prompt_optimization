"""Validating an external step (EXTERNAL-STEPS.md §8).

Firing junk at an endpoint and checking it returns 200 tells you the server is up and nothing else. The failures
that cost money are about the **relationship between the step's output and the label**, and about **stability** -
and none of the checks here cost a model call. They are arithmetic over columns.

Tier 0 is static: does the wiring make sense at all.
Tier 2 is statistical: is the data this step returns worth fetching, and is it what you think it is.
(Tier 1 - reachability, auth, schema, how the service says "no record" - is a live probe, in `main.py`.
 Tier 3 is the existing pilot with the step switched on.)

Findings use the studio's existing `Issue` shape, so they render in the validation panel already built and an
`error` blocks Run exactly as a mapping error does.
"""
from __future__ import annotations

import math
import random
from collections import Counter
from typing import Any

from .models import ProjectSpec, ValidationReport
from .steps import column, load_manifest

MIN_COVERAGE = 0.5          # below this the step cannot teach what it is there to teach
WARN_COVERAGE = 0.9
LEAK_ACCURACY = 0.98        # a single field that predicts the label this well is the label
SIGNAL_P = 0.05             # permutation test
PERMUTATIONS = 200


# ---- tier 0: static -----------------------------------------------------------------------------

def check_scopes(spec: ProjectSpec, connections: list[dict], rep: ValidationReport) -> None:
    """An OAuth step declares the scopes it needs; the grant records what was actually given. Comparing them here
    turns a 403 on row 1 of an enrichment into a sentence before anything is spent."""
    by_id = {c["id"]: c for c in connections}
    for s in spec.steps:
        if not s.enabled or not (s.credential or "").startswith("conn:"):
            continue
        m = load_manifest(s.manifest)
        if m is None or not m.oauth_provider:
            continue
        conn = by_id.get(s.credential.split(":", 1)[1])
        if conn is None:
            rep.add("error", "step", "the connection this step points at no longer exists; connect again", where=s.id)
            continue
        if conn["provider"] != m.oauth_provider:
            rep.add("error", "step", f"this step needs a {m.oauth_provider} connection but points at a {conn['provider']} one", where=s.id)
            continue
        missing = [x for x in m.required_scopes if x not in (conn.get("scopes") or "").split()]
        if missing:
            rep.add("error", "step", f"the {conn['provider']} connection was not granted {missing}: reconnect and approve "
                                     f"those permissions", where=s.id, missing=missing)


def check_steps(spec: ProjectSpec, dataset_columns: list[str], rep: ValidationReport) -> None:
    from bpto import Prompt
    seen_out: dict[str, str] = {}
    for s in spec.steps:
        if not s.enabled:
            continue
        m = load_manifest(s.manifest)
        if m is None:
            rep.add("error", "step", f"no manifest {s.manifest!r} is installed (or its version has moved)", where=s.id)
            continue
        for f in m.inputs:
            col = s.inputs.get(f.name)
            if f.required and not col:
                rep.add("error", "step", f"required input {f.name!r} is not mapped to a column", where=s.id)
            elif col and col not in dataset_columns:
                rep.add("error", "step", f"input {f.name!r} is mapped to column {col!r}, which the dataset does not have", where=s.id)
        if m.cacheable and not set(m.cache_key) <= {f.name for f in m.inputs}:
            rep.add("error", "step", f"the manifest's cache key {m.cache_key} is not a subset of its inputs", where=s.id)
        for f in m.outputs:
            col = column(s.id, f.name)
            # a collision only counts when the column is not ours: once enriched, the dataset carries these by
            # design, and the marker column is how we tell "we froze this" from "it was already called that"
            if col in dataset_columns and column(s.id, "_captured_at") not in dataset_columns:
                rep.add("error", "step", f"output column {col!r} collides with a column the dataset already has", where=s.id)
            if col in seen_out:
                rep.add("error", "step", f"two steps both provide {col!r} ({seen_out[col]} and {s.id})", where=s.id)
            seen_out[col] = s.id
        # what counts as authorised depends on how the manifest says the step signs in (GLOSSARY.md)
        cred, kind = s.credential or "", m.auth.get("kind")
        if not m.needs_auth:
            pass
        elif kind == "oauth":
            if not cred.startswith("conn:"):
                rep.add("error", "step", f"this step acts on your own {m.oauth_provider} account, so it needs a "
                                         f"connection rather than a key: connect it from this step, or under Models & keys",
                        where=s.id)
        elif kind in ("key", "bearer"):
            if not cred.startswith("step:"):
                rep.add("error", "step", "this step is not authorised: paste a step key for it", where=s.id)
        elif not cred:
            rep.add("error", "step", f"this step is not authorised: connect your {m.oauth_provider} account, or paste "
                                     f"a step key", where=s.id)
        used = set()
        for mod in spec.modules:
            try:
                used |= set(Prompt(template=mod.template).placeholders)
            except ValueError:
                pass
        unused = [f.name for f in m.outputs if column(s.id, f.name) not in used]
        if len(unused) == len(m.outputs):
            rep.add("warn", "step", "no prompt uses anything this step returns: you are paying for data nobody reads", where=s.id)
        elif unused:
            # This is a data-hygiene finding, not a cost one. The call returns every declared field in one response,
            # so an unused field costs no extra call, and it is not in the prompt, so it costs no tokens either.
            # What it does cost is data you are moving without a reason - which matters when the field is personal.
            rep.add("warn", "step", f"no prompt reads {unused}. That costs no extra call and no tokens, but you are pulling "
                                    f"data you do not use: drop it from the manifest's outputs, or check it is not personal "
                                    f"data you have no reason to hold", where=s.id, unused=unused)


def check_frozen(spec: ProjectSpec, dataset_columns: list[str], rep: ValidationReport) -> None:
    """Evaluation reads the frozen snapshot, so the dataset must actually carry it. Separate from `check_steps`
    because the fix is different: enrich the dataset, rather than rewire the step."""
    for s in spec.steps:
        if not s.enabled:
            continue
        m = load_manifest(s.manifest)
        if m is None:
            continue
        missing = [column(s.id, f.name) for f in m.outputs if column(s.id, f.name) not in dataset_columns]
        if missing:
            rep.add("error", "step", f"this dataset has not been enriched by {s.id!r}: run 'Fetch and freeze' on the "
                                     f"Data tab (missing {missing[:3]}{'…' if len(missing) > 3 else ''})", where=s.id)


# ---- tier 2: statistics over the frozen columns --------------------------------------------------

def _entropy(counts) -> float:
    n = sum(counts)
    return -sum((c / n) * math.log2(c / n) for c in counts if c) if n else 0.0


def _majority_accuracy(pairs: list[tuple[Any, Any]], folds: int = 5) -> float:
    """Accuracy of the simplest possible use of a field: answer each value's most common label.

    Cross-validated, and that is not fussiness. Fitted and scored on the same rows, this statistic rewards any field
    with many distinct values - a random number binned into twenty groups "predicts" beautifully, because each group
    is small enough to memorise. Scoring each fold with a rule built from the other folds removes that: a noise field
    has nothing to say about rows it has not seen. Groups absent from the training folds fall back to the overall
    majority, which is what a caller would do anyway.
    """
    n = len(pairs)
    if n < folds * 2:
        folds = 1
    if folds == 1:                                   # too little data to hold anything out; say so by not pretending
        by: dict[Any, Counter] = {}
        for v, y in pairs:
            by.setdefault(v, Counter())[y] += 1
        return sum(c.most_common(1)[0][1] for c in by.values()) / max(1, n)
    correct = 0
    for f in range(folds):
        train = [pairs[i] for i in range(n) if i % folds != f]
        by = {}
        for v, y in train:
            by.setdefault(v, Counter())[y] += 1
        overall = Counter(y for _, y in train).most_common(1)[0][0] if train else None
        rule = {v: c.most_common(1)[0][0] for v, c in by.items()}
        correct += sum(1 for i in range(n) if i % folds == f and rule.get(pairs[i][0], overall) == pairs[i][1])
    return correct / max(1, n)


def check_signal(spec: ProjectSpec, rows: list[dict[str, Any]], label_column: str | None, rep: ValidationReport,
                 seed: int = 0) -> None:
    """Does what this step returns help predict the label?

    Two tests, because one is not enough. A **marginal** test (this field alone) is what catches an obviously
    useless field, but it is blind to interaction: in the entitlement case the queue depends on the ticket text AND
    the customer's plan together, so `plan` alone looks like noise while being the whole point. So the fields are
    also tested **jointly**, and only a step that fails both is called out.

    Neither test can see the input text - that is Tier 3's job, the pilot with the step on and off. What these can
    do for free is stop you spending on a step that carries nothing, and catch one that carries the answer.
    """
    if not label_column or not rows or not spec.steps:
        return
    labels = [r.get(label_column) for r in rows]
    if len(set(labels)) < 2:
        return
    base = Counter(labels).most_common(1)[0][1] / len(labels)     # always answer the majority class
    rnd = random.Random(seed)

    def permutation_p(groups: list[Any], acc: float) -> float:
        """How often does the same grouping do this well on shuffled labels? Handles a fine-grained grouping
        honestly: many small groups score high on real and shuffled labels alike, so p stays large."""
        hits = 0
        for _ in range(PERMUTATIONS):
            shuffled = labels[:]
            rnd.shuffle(shuffled)
            if _majority_accuracy(list(zip(groups, shuffled))) >= acc:
                hits += 1
        return (hits + 1) / (PERMUTATIONS + 1)

    for s in spec.steps:
        if not s.enabled:
            continue
        m = load_manifest(s.manifest)
        usable: list[list[Any]] = []
        names: list[str] = []
        quiet: list[str] = []
        for f in (m.outputs if m else []):
            col = column(s.id, f.name)
            vals = [r.get(col) for r in rows]
            if all(v is None for v in vals):
                continue
            distinct = len({str(v) for v in vals})
            if distinct <= 1:
                rep.add("warn", "step", f"{col} is the same on every row: it carries no information and costs tokens on every call", where=s.id)
                continue
            if distinct >= 0.95 * len(rows):
                rep.add("warn", "step", f"{col} is unique on almost every row (an id or a timestamp): nothing can be learned from it, and it costs tokens", where=s.id)
                continue
            binned = [_bin(v) for v in vals]
            acc = _majority_accuracy(list(zip(binned, labels)))
            if acc >= LEAK_ACCURACY:
                rep.add("error", "step", f"{col} predicts the label {acc:.0%} of the time on its own. That is not a "
                                         f"useful feature, it is the answer: check the step is not returning the label", where=s.id)
                continue
            usable.append(binned)
            names.append(col)
            p = permutation_p(binned, acc)
            if p > SIGNAL_P:
                quiet.append(col)
                rep.add("info", "step", f"{col} has no relationship to the label on its own ({acc:.0%} against a {base:.0%} "
                                        f"baseline, p={p:.2f}). It may still matter combined with the input text, which this check cannot see",
                        where=s.id, accuracy=round(acc, 3), baseline=round(base, 3))
            else:
                rep.add("info", "step", f"{col} carries signal on its own: {acc:.0%} against a {base:.0%} baseline (p={p:.3f})",
                        where=s.id, accuracy=round(acc, 3), baseline=round(base, 3))

        # ... and together, which is the test that sees an interaction between the step's own fields
        if len(usable) > 1:
            joint = [tuple(col[i] for col in usable) for i in range(len(rows))]
            acc = _majority_accuracy(list(zip(joint, labels)))
            p = permutation_p(joint, acc)
            groups = len(set(joint))
            if p > SIGNAL_P:
                rep.add("warn", "step", f"nothing this step returns predicts the label, alone or in combination "
                                        f"({acc:.0%} against a {base:.0%} baseline, p={p:.2f}, {groups} distinct combinations). "
                                        f"Run the pilot with the step on and off before spending a run on it", where=s.id)
            else:
                rep.add("info", "step", f"taken together, this step's fields predict the label {acc:.0%} of the time against a "
                                        f"{base:.0%} baseline (p={p:.3f}) - the interaction the per-field numbers above cannot show",
                        where=s.id, accuracy=round(acc, 3), baseline=round(base, 3))
                # leave-one-out: which of them is actually carrying that? A field whose removal costs nothing carries
                # nothing the others do not already carry, and is the first thing to drop from the prompt.
                dead = []
                for i, name in enumerate(names):
                    rest = [c for j, c in enumerate(usable) if j != i]
                    without = [tuple(col[k] for col in rest) for k in range(len(rows))]
                    delta = acc - _majority_accuracy(list(zip(without, labels)))
                    if delta <= 0:
                        dead.append(name)
                    else:
                        rep.add("info", "step", f"{name} is worth {delta:+.0%} of the {acc:.0%}: the other fields cannot replace it",
                                where=s.id, delta=round(delta, 3))
                if dead:
                    rep.add("warn", "step", f"{dead} add nothing the step's other fields do not already carry. Dropping them from the "
                                            f"prompt costs tokens on every call and (on this data) no accuracy - though this arithmetic "
                                            f"cannot see the input text, so confirm with a run before trusting it",
                            where=s.id, redundant=dead)
def _bin(v: Any) -> Any:
    """Numbers get coarse bins so a continuous field is not automatically 'unique on every row'."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return "none" if v == 0 else f"~{10 ** int(math.log10(abs(v)))}" if abs(v) >= 1 else "<1"
    return v


def check_coverage(spec: ProjectSpec, rows: list[dict[str, Any]], rep: ValidationReport) -> None:
    """How many rows actually got a record. Below the floor the joint rule is unlearnable for the rest of them, and
    the user should know that before spending anything."""
    for s in spec.steps:
        if not s.enabled:
            continue
        m = load_manifest(s.manifest)
        if m is None or not m.outputs:
            continue
        first = column(s.id, m.outputs[0].name)
        err = column(s.id, "_error")
        if not rows or first not in rows[0]:
            continue
        got = sum(1 for r in rows if r.get(first) is not None)
        failed = sum(1 for r in rows if r.get(err))
        cov = got / len(rows)
        where = s.id
        if failed:
            rep.add("warn", "step", f"{failed} of {len(rows)} rows failed to fetch and carry no data", where=where)
        if cov < MIN_COVERAGE:
            rep.add("error", "step", f"only {cov:.0%} of rows have a record. Whatever this step is meant to teach, "
                                     f"most rows cannot teach it", where=where, coverage=round(cov, 3))
        elif cov < WARN_COVERAGE:
            rep.add("warn", "step", f"{cov:.0%} of rows have a record; the rest fall back to whatever the message alone says",
                    where=where, coverage=round(cov, 3))


def age_note(spec: ProjectSpec, rows: list[dict[str, Any]], rep: ValidationReport, max_age_days: float = 30.0) -> None:
    """The snapshot is frozen on purpose; stale is still stale. This is the alert that makes the asymmetry between
    training and production visible rather than silent."""
    import time
    for s in spec.steps:
        col = column(s.id, "_captured_at")
        ts = [r[col] for r in rows if isinstance(r.get(col), (int, float))]
        if not ts:
            continue
        age = (time.time() - min(ts)) / 86400
        if age > max_age_days:
            rep.add("warn", "step", f"this step's data was captured {age:.0f} days ago. Labels were given against the "
                                    f"records as they were then; re-freeze if the world has moved", where=s.id, age_days=round(age, 1))
        else:
            rep.add("info", "step", f"data captured {age:.1f} days ago", where=s.id, age_days=round(age, 2))
