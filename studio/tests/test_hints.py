"""The in-app hints point at the published docs, so this asserts the two have not drifted.

A hint is a preview of a doc section plus a link to it. If the link rots, the hint becomes the only copy of the
explanation and starts growing its own version of the truth - which is the failure this file exists to prevent.
It also holds the budget: eight. A ninth hint means retiring one, not raising the number here quietly.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HINTS = REPO / "studio/web/src/Hint.jsx"
DOCS = REPO / "impromptune/docs"

BUDGET = 8


def hints() -> dict[str, str]:
    src = HINTS.read_text()
    body = src[src.index("export const HINTS"):src.index("export default function")]
    return dict(re.findall(r"^  (\w+): \{.*?^    doc: '([^']+)',", body, re.S | re.M))


def test_every_hint_is_declared_once_and_the_budget_holds():
    h = hints()
    assert h, "no hints parsed - has Hint.jsx changed shape?"
    assert len(h) <= BUDGET, f"{len(h)} hints, budget is {BUDGET}: retire one rather than raising the budget"
    assert len(h) == len(re.findall(r"^  \w+: \{", HINTS.read_text(), re.M))


@pytest.mark.parametrize("name,doc", sorted(hints().items()))
def test_the_page_and_anchor_a_hint_links_to_exist(name, doc):
    path, _, anchor = doc.partition("#")
    page = DOCS / (path.removeprefix("/docs/") + ".html")
    assert page.exists(), f"hint {name!r} links to {doc}, but {page.relative_to(REPO)} does not exist"
    if anchor:
        assert f'id="{anchor}"' in page.read_text(), f"hint {name!r} links to #{anchor}, which {page.name} has no id for"


def test_every_hint_is_actually_placed_in_the_ui():
    used = set()
    for f in (REPO / "studio/web/src").glob("*.jsx"):
        if f.name != "Hint.jsx":
            used |= set(re.findall(r'<Hint id="(\w+)"', f.read_text()))
    assert used == set(hints()), f"declared but unplaced: {set(hints()) - used}; placed but undeclared: {used - set(hints())}"
