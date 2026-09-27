"""Retrieval documents: parsing, limits and the chunker. Storage is `store.py`'s (the corpora section); this module
decides what a document becomes before it is stored.

The chunker is heading-aware markdown: a chunk never spans two sections, so every chunk carries the heading path it
sits under ("Changing plans > Downgrading"), which is what makes a short chunk retrievable and a retrieved one
legible. Tables and code blocks are kept whole (a table split mid-row answers nothing); only a table far over the
budget is split, by rows, with its header repeated. Plain text is the same with no headings.

Token counts are an estimate (characters / 4): they size chunks, they are not billed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

# Per corpus (BETA.md, Caps and limits). They cap embedding spend and the size of any one retrieved context.
MAX_FILES = 50  # BETA.md proposed 20; our own knowledge-base template has 32. Bytes and chunks bound the spend
MAX_BYTES = 10 * 1024 * 1024
MAX_CHUNKS = 2000
EXTENSIONS = (".md", ".markdown", ".txt")

MAX_TOKENS = 300
OVERLAP_TOKENS = 40
TABLE_MAX_TOKENS = 3 * MAX_TOKENS
CHUNKER = f"md-v1:{MAX_TOKENS}/{OVERLAP_TOKENS}"  # recorded per document: re-chunking changes what a version retrieves


def approx_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


@dataclass
class Chunk:
    heading: str
    text: str
    tokens: int

    def passage(self, document: str = "") -> str:
        """What retrieval hands the prompt and what gets embedded: where the text came from, then the text."""
        where = " > ".join(x for x in (document, self.heading) if x)
        return f"[{where}]\n{self.text}" if where else self.text


# ---- blocks -------------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*_(\"'])")


def _blocks(text: str) -> list[tuple[str, str]]:
    """(kind, text) in reading order; kind is heading:<level>, table, code or text."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[0].strip() == "---":  # front matter is metadata, not content
        for i in range(1, len(lines)):
            if lines[i].strip() in ("---", "..."):
                lines = lines[i + 1:]
                break
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    kind = "text"

    def flush():
        nonlocal buf
        if any(l.strip() for l in buf):
            out.append((kind, "\n".join(buf).strip("\n")))
        buf = []

    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            flush()
            fence = _FENCE.match(line).group(1)
            code = [line]
            i += 1
            while i < len(lines):
                code.append(lines[i])
                if lines[i].strip().startswith(fence):
                    break
                i += 1
            out.append(("code", "\n".join(code)))
            i += 1
            kind = "text"
            continue
        m = _HEADING.match(line)
        if m:
            flush()
            out.append((f"heading:{len(m.group(1))}", m.group(2)))
            kind = "text"
        elif line.lstrip().startswith("|"):
            if kind != "table":
                flush()
                kind = "table"
            buf.append(line)
        elif not line.strip():
            flush()
            kind = "text"
        else:
            if kind == "table":
                flush()
                kind = "text"
            buf.append(line)
        i += 1
    flush()
    return out


# ---- splitting one oversized block ----------------------------------------------------------

def _split_words(text: str, budget: int) -> list[str]:
    parts, cur = [], ""
    for w in text.split(" "):
        if cur and approx_tokens(cur + " " + w) > budget:
            parts.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    return parts + ([cur] if cur else [])


def _split_text(text: str, budget: int) -> list[str]:
    parts, cur = [], ""
    for s in _SENTENCE.split(text):
        if approx_tokens(s) > budget:
            if cur:
                parts.append(cur)
                cur = ""
            parts.extend(_split_words(s, budget))
        elif cur and approx_tokens(cur + " " + s) > budget:
            parts.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}" if cur else s
    return parts + ([cur] if cur else [])


def _split_table(text: str, budget: int) -> list[str]:
    rows = text.split("\n")
    head = rows[:2] if len(rows) > 1 and set(rows[1].replace("|", "").strip()) <= set("-: ") else rows[:1]
    body = rows[len(head):]
    parts, cur = [], []
    for r in body:
        if cur and approx_tokens("\n".join(head + cur + [r])) > budget:
            parts.append("\n".join(head + cur))
            cur = []
        cur.append(r)
    return parts + (["\n".join(head + cur)] if cur else [])


def _pieces(kind: str, text: str, max_tokens: int) -> list[tuple[str, str]]:
    if kind == "table":
        return [("table", text)] if approx_tokens(text) <= TABLE_MAX_TOKENS else [("table", t) for t in _split_table(text, max_tokens)]
    if kind == "code" or approx_tokens(text) <= max_tokens:
        return [(kind, text)]
    return [("text", t) for t in _split_text(text, max_tokens)]


def _tail(text: str, budget: int) -> str:
    """The last whole sentences of `text` that fit `budget`: the overlap carried into the next chunk."""
    out = ""
    for s in reversed(_SENTENCE.split(text)):
        cand = f"{s} {out}".strip()
        if approx_tokens(cand) > budget:
            break
        out = cand
    return out


# ---- chunking ------------------------------------------------------------------------------

def chunk(text: str, *, max_tokens: int = MAX_TOKENS, overlap_tokens: int = OVERLAP_TOKENS) -> list[Chunk]:
    chunks: list[Chunk] = []
    stack: list[tuple[int, str]] = []
    section: list[tuple[str, str]] = []

    def close_section():
        heading = " > ".join(t for _, t in stack)
        cur: list[tuple[str, str]] = []

        def emit():
            body = "\n\n".join(t for _, t in cur)
            if body.strip():
                chunks.append(Chunk(heading, body, approx_tokens(body)))

        for kind, text in section:
            for k, t in _pieces(kind, text, max_tokens):
                if cur and approx_tokens("\n\n".join([x for _, x in cur] + [t])) > max_tokens:
                    emit()
                    last_kind, last = cur[-1]
                    tail = _tail(last, overlap_tokens) if last_kind == "text" and overlap_tokens else ""
                    cur = [("text", tail)] if tail and tail != t else []
                cur.append((k, t))
        emit()
        section.clear()

    for kind, text in _blocks(text):
        if kind.startswith("heading:"):
            close_section()
            level = int(kind.split(":")[1])
            stack[:] = [(l, t) for l, t in stack if l < level] + [(level, text)]
        else:
            section.append((kind, text))
    close_section()
    return chunks


# ---- documents ------------------------------------------------------------------------------

class DocumentError(ValueError):
    """A file the corpus refuses; the message is shown to the user as is."""


def read_document(name: str, data: bytes) -> tuple[str, str, str]:
    """-> (text, extension, sha256). Refuses what the chunker cannot read."""
    ext = next((e for e in EXTENSIONS if name.lower().endswith(e)), None)
    if ext is None:
        raise DocumentError(f"{name}: only {', '.join(EXTENSIONS)} files for now")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DocumentError(f"{name}: not UTF-8 text")
    if not text.strip():
        raise DocumentError(f"{name}: the file is empty")
    return text, ext, hashlib.sha256(data).hexdigest()


def check_limits(*, n_files: int, n_bytes: int, n_chunks: int) -> None:
    """Totals for the corpus as it would be after the upload."""
    if n_files > MAX_FILES:
        raise DocumentError(f"a document set holds at most {MAX_FILES} files")
    if n_bytes > MAX_BYTES:
        raise DocumentError(f"a document set holds at most {MAX_BYTES // (1024 * 1024)} MB")
    if n_chunks > MAX_CHUNKS:
        raise DocumentError(f"a document set holds at most {MAX_CHUNKS} passages; this upload would make {n_chunks}")


def chunk_dicts(text: str) -> list[dict]:
    return [asdict(c) for c in chunk(text)]


# ---- embedding ---------------------------------------------------------------------------------

def embed_text(c: dict) -> str:
    """What a chunk is embedded as: its heading path, then its text (the document name adds nothing the H1 lacks)."""
    return Chunk(c["heading"], c["text"], c["tokens"]).passage()


def embedding_status(con, corpus_id: str, user_id: str, model: str) -> dict:
    from . import embedding as E, store
    docs = store.list_documents(con, corpus_id, user_id)
    have = store.vector_documents(con, corpus_id, model)
    pending = [d for d in docs if d["id"] not in have]
    # Counted on what is embedded, heading line included: 5,204 estimated vs Titan's 5,024 on the kb corpus
    tokens = sum(approx_tokens(embed_text(c)) for d in pending for c in store.document_chunks(con, d["id"], user_id))
    return {"model": model, "embedded": len(docs) - len(pending), "pending": len(pending),
            "pending_tokens_est": tokens, "pending_usd_est": E.estimate_usd(tokens, model)}


async def embed_pending(con, corpus_id: str, user_id: str, embedder) -> dict:
    """Embeds every document in the corpus that has no vectors for `embedder.model`, in one `embed()` call (one
    usage row). Documents that already have them are not re-embedded."""
    from . import store
    have = store.vector_documents(con, corpus_id, embedder.model)
    todo = [(d, store.document_chunks(con, d["id"], user_id)) for d in store.list_documents(con, corpus_id, user_id) if d["id"] not in have]
    texts = [embed_text(c) for _, cs in todo for c in cs]
    vecs = await embedder.embed(texts) if texts else []
    i = 0
    for d, cs in todo:
        store.put_vectors(con, document_id=d["id"], corpus_id=corpus_id, model=embedder.model, vectors=vecs[i:i + len(cs)])
        i += len(cs)
    return {"model": embedder.model, "documents": len(todo), "chunks": len(texts), "usd": embedder.budget.spent_usd}


# ---- retrieval: the function behind the "Search your documents" step ---------------------------

DEFAULT_K = 4


def make_retriever(con, corpus_id: str, user_id: str, embedder, k: int = DEFAULT_K, document_ids: list[str] | None = None):
    """-> async (inputs) -> (outputs | None, usd). Loads the vectors once, then per call embeds the query and returns
    the top-k chunks by cosine (vectors are normalised, so a dot product). `document_ids` is a published version's
    pin: exactly those documents, whatever the set holds now; None (the canvas) means the set as it stands.
    Refuses rather than search part of what was asked for: a document without vectors, or a pinned one deleted."""
    import numpy as np
    from . import store
    from .steps import StepError
    if document_ids is not None:
        present = {c["document_id"] for c in store.corpus_chunks(con, corpus_id, user_id, document_ids)}
        gone = [d for d in document_ids if d not in present]
        if gone:
            raise StepError(f"this version searches {len(gone)} document(s) that have since been deleted: publish a new version")
        pending = [d for d in document_ids if d not in store.vector_documents(con, corpus_id, embedder.model)]
    else:
        pending = [None] * embedding_status(con, corpus_id, user_id, embedder.model)["pending"]
    if pending:
        raise StepError(f"{len(pending)} document(s) in this set are not searchable yet: open the set and retry embedding")
    chunks, mat = store.load_vectors(con, corpus_id, user_id, embedder.model, document_ids)
    return retriever_over(chunks, mat, embedder, k)


def retriever_over(chunks: list[dict], mat, embedder, k: int = DEFAULT_K):
    """The search itself, over chunks (dicts with document, heading, text, tokens) and their vectors, rows aligned.
    Shared by the studio and the serving box, so a hosted version formats its passages byte for byte as evaluation did."""
    import numpy as np
    from .steps import StepError
    if not chunks:
        raise StepError("this document set is empty")
    mat = np.asarray(mat, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = mat / np.where(norms == 0, 1, norms)
    k = max(1, min(int(k or DEFAULT_K), len(chunks)))

    async def retrieve(inputs: dict) -> tuple[dict | None, float]:
        q = str(inputs.get("query") or "").strip()
        if not q:
            return None, 0.0
        before = embedder.budget.spent_usd
        v = np.asarray((await embedder.embed([q]))[0], dtype=np.float32)
        v /= np.linalg.norm(v) or 1.0
        top = np.argsort(-(mat @ v), kind="stable")[:k]
        picked = [chunks[i] for i in top]
        context = "\n\n".join(Chunk(c["heading"], c["text"], c["tokens"]).passage(c["document"]) for c in picked)
        sources = "; ".join(f"{c['document']}#{c['heading'].split(' > ')[-1]}" if c["heading"] else c["document"] for c in picked)
        return {"context": context, "sources": sources}, embedder.budget.spent_usd - before

    return retrieve


# ---- bundles: a document set that travels with a project ------------------------------------------

def _b64(arr) -> str:
    import base64
    import numpy as np
    return base64.b64encode(np.asarray(arr, dtype="<f4").tobytes()).decode()


def _unb64(s: str, dims: int):
    import base64
    import numpy as np
    return np.frombuffer(base64.b64decode(s), dtype="<f4").reshape(-1, dims)


def export_corpus(con, corpus_id: str, user_id: str) -> dict:
    """The set as it stands, as bundle data: every document's text, and its vectors per embed model, so whoever
    imports it searches without paying to embed again. The offline hash vectors are left out: they cost nothing to
    recompute."""
    import numpy as np
    from . import embedding as E, store
    c = store.get_corpus(con, corpus_id, user_id)
    docs = store.list_documents(con, corpus_id, user_id)
    out = {"name": c["name"], "chunker": CHUNKER, "documents": [], "vectors": {}}
    for d in docs:
        out["documents"].append({"name": d["name"], "text": store.blob_path(d["blob"]).read_text(encoding="utf-8-sig")})
    for r in con.execute("select document_id, model, dims, blob from corpus_vectors where corpus_id=?", (corpus_id,)):
        name = next((d["name"] for d in docs if d["id"] == r["document_id"]), None)
        if name is None or r["model"] == E.HASH:
            continue
        v = out["vectors"].setdefault(r["model"], {"dims": r["dims"], "documents": {}})
        v["documents"][name] = _b64(np.load(store.blob_path(r["blob"]), allow_pickle=False))
    return out


async def import_corpus(con, user_id: str, data: dict) -> dict:
    """Creates a document set from bundle data. Shipped vectors are used only when they were made by this chunker
    and line up with the chunks, so a template built before a chunker change re-embeds instead of misaligning.
    Hash vectors are always computed: free, offline, and what the mock searches with."""
    from . import embedding as E, store
    docs = data.get("documents") or []
    staged = []
    for d in docs:
        raw = d["text"].encode("utf-8")
        text, ext, sha = read_document(d["name"], raw)
        chunks = chunk_dicts(text)
        if not chunks:
            raise DocumentError(f"{d['name']}: no text to search")
        staged.append((d["name"], raw, ext, sha, chunks))
    check_limits(n_files=len(staged), n_bytes=sum(len(s[1]) for s in staged), n_chunks=sum(len(s[4]) for s in staged))
    c = store.create_corpus(con, user_id=user_id, name=data.get("name") or "documents")
    same_chunker = data.get("chunker") == CHUNKER
    reused = {}
    for name, raw, ext, sha, chunks in staged:
        doc = store.add_document(con, corpus_id=c["id"], user_id=user_id, name=name, sha256=sha, data=raw, ext=ext,
                                 chunker=CHUNKER, chunks=chunks)
        for model, v in (data.get("vectors") or {}).items():
            b = (v.get("documents") or {}).get(name)
            if not (same_chunker and b and model != E.HASH):
                continue
            arr = _unb64(b, int(v["dims"]))
            if len(arr) == len(chunks):
                store.put_vectors(con, document_id=doc["id"], corpus_id=c["id"], model=model, vectors=arr)
                reused[model] = reused.get(model, 0) + 1
    await embed_pending(con, c["id"], user_id, E.make_embedder(mock=True, purpose="import"))
    return {"id": c["id"], "name": c["name"], "documents": len(staged), "vectors_reused": reused}
