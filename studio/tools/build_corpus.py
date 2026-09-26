"""Build a library document set: a folder of .md/.txt files -> `<out>.corpus.json` (the bundle `corpus` shape), with
Titan vectors so a clone searches without embedding anything.

    python tools/build_corpus.py DOCS_DIR OUT.corpus.json --name "Help centre" [--mock]

Live embedding bills the house Bedrock account (Titan v2, $0.02 per 1M tokens; the estimate prints first). Runs in
a throwaway data directory: nothing touches the studio's database.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("docs", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--name", default="documents")
    ap.add_argument("--mock", action="store_true", help="offline hash vectors only (the file then carries none)")
    ap.add_argument("--max-usd", type=float, default=0.05)
    a = ap.parse_args()
    os.environ["STUDIO_DATA"] = tempfile.mkdtemp(prefix="corpus-build-")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.main import load_env
    load_env()
    from app import corpora as K, db, embedding as E, store

    con = db.connect()
    store.init(con)
    files = sorted(p for p in a.docs.iterdir() if p.suffix.lower() in K.EXTENSIONS)
    data = {"name": a.name, "chunker": K.CHUNKER, "documents": [{"name": p.name, "text": p.read_text(encoding="utf-8-sig")} for p in files]}
    imp = asyncio.run(K.import_corpus(con, "builder", data))  # same checks and chunking a user's upload gets
    st = K.embedding_status(con, imp["id"], "builder", E.TITAN)
    print(f"{len(files)} documents, {sum(d['n_chunks'] for d in store.list_documents(con, imp['id'], 'builder'))} chunks; "
          f"Titan estimate {st['pending_tokens_est']} tokens = ${st['pending_usd_est']:.5f}")
    if not a.mock:
        emb = E.make_embedder(mock=False, purpose="build", max_usd=a.max_usd)
        got = asyncio.run(K.embed_pending(con, imp["id"], "builder", emb))
        print(f"embedded {got['chunks']} chunks, ${got['usd']:.5f} ({emb.budget.spent.input_tokens} tokens)")
    a.out.write_text(json.dumps(K.export_corpus(con, imp["id"], "builder")))
    print(f"wrote {a.out} ({a.out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
