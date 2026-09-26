"""The chunker and the document checks: pure functions, no data directory (the API flow is in test_api.py)."""
from __future__ import annotations

import pytest

from app import corpora as K

DOC = """---
title: ignored
---
# Refunds

Intro line.

## Annual plans

Refunds on annual plans are paid as account credit. They are never paid to the card.

| Plan | Window |
|---|---|
| Pro | 45 days |
| Basic | 14 days |

## Monthly plans

```
code stays whole

even with a blank line
```
"""


def test_chunks_follow_headings_and_keep_tables_and_code_whole():
    cs = K.chunk(DOC)
    assert [c.heading for c in cs] == ["Refunds", "Refunds > Annual plans", "Refunds > Monthly plans"]
    assert "title: ignored" not in "".join(c.text for c in cs)
    annual = cs[1].text
    assert "| Pro | 45 days |\n| Basic | 14 days |" in annual and annual.startswith("Refunds on annual")
    assert "even with a blank line" in cs[2].text and cs[2].text.startswith("```")
    assert cs[1].passage("refunds.md") == f"[refunds.md > Refunds > Annual plans]\n{annual}"


def test_long_text_is_split_under_budget_with_overlap():
    sentences = [f"Sentence number {i} says something about refunds and seats." for i in range(80)]
    cs = K.chunk("# Long\n\n" + " ".join(sentences), max_tokens=100, overlap_tokens=30)
    assert len(cs) > 3 and all(c.heading == "Long" for c in cs)
    assert all(c.tokens <= 100 + 30 for c in cs)
    for a, b in zip(cs, cs[1:]):  # each chunk opens with the end of the one before
        assert b.text.split(". ")[0].rstrip(".") in a.text
    assert all(s in " ".join(c.text for c in cs) for s in sentences)


def test_huge_table_splits_by_rows_with_the_header_repeated():
    rows = "\n".join(f"| item {i} | {i * 3} dollars and some description text |" for i in range(300))
    cs = K.chunk("# Prices\n\n| Item | Price |\n|---|---|\n" + rows)
    assert len(cs) > 1
    assert all(c.text.startswith("| Item | Price |\n|---|---|\n") for c in cs)
    body = [l for c in cs for l in c.text.split("\n")[2:]]
    assert body == rows.split("\n")


def test_plain_text_and_empty_sections():
    cs = K.chunk("First paragraph.\n\nSecond paragraph.")
    assert len(cs) == 1 and cs[0].heading == "" and "Second" in cs[0].text
    assert K.chunk("# Only a heading\n\n## Another") == []


def test_read_document_and_limits():
    text, ext, sha = K.read_document("a.MD", "﻿# Hi\n".encode("utf-8"))
    assert text == "# Hi\n" and ext == ".md" and len(sha) == 64
    for name, data in [("a.pdf", b"%PDF"), ("a.txt", b"\xff\xfe\x00"), ("a.txt", b"  \n")]:
        with pytest.raises(K.DocumentError):
            K.read_document(name, data)
    K.check_limits(n_files=K.MAX_FILES, n_bytes=K.MAX_BYTES, n_chunks=K.MAX_CHUNKS)
    for kw in [dict(n_files=K.MAX_FILES + 1, n_bytes=0, n_chunks=0), dict(n_files=1, n_bytes=K.MAX_BYTES + 1, n_chunks=0),
               dict(n_files=1, n_bytes=0, n_chunks=K.MAX_CHUNKS + 1)]:
        with pytest.raises(K.DocumentError):
            K.check_limits(**kw)
