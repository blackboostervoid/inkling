# @module: semantic
# @role: On-demand Tier-2 semantic search over the wiki — the meaning-based complement to the lexical recall_scanner. NOT on the hot path: the agent invokes it by hand when lexical [RECALL] missed and the query is conceptual (relevant note, no shared surface words).
# @deps: stdlib + urllib (zero third-party); ollama + bge-m3 embedding model (local HTTP)
# @used-by: invoked manually by the agent via Bash (see CLAUDE.md Tier-2 note); not wired to any hot-path hook
# @doc: harvested from obsidian-mind QMD. Kept OFF the per-prompt hook (embedding latency + cross-machine model sync would break the hot path). bge-m3 chosen for strong multilingual coverage; pull it in ollama first.
"""
semantic.py — lexical recall's meaning-based twin, on demand.

recall_scanner (Go, hot path) matches when conversation words overlap a note's tags/summary. It
misses the "relevant note, zero shared words" case — ask about "retry logic", the note is titled
"circuit breaker". This closes that gap WITHOUT touching the hot path: it is a manual tool the agent
runs when it suspects a lexical miss, not a per-prompt hook.

  py semantic.py index                 # (re)build the vector index incrementally
  py semantic.py search "<query>" [-k N]   # refresh, then cosine top-K

Engine: ollama + bge-m3 (1024-dim, multilingual, which a vault in any language needs), embedded over each
note's name+tags+summary from the compiled corpus. Vectors are L2-normalized and cached in
semantic_index.json keyed by path+text-hash, so a rebuild only re-embeds notes whose essence
changed. ollama down / model missing -> graceful message, exit 0, fall back to lexical.

Stdlib only. Invoke via `py`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("CLAUDE_EMBED_MODEL", "bge-m3")
EMBED_BATCH = 24
DEFAULT_K = 8


class OllamaUnavailable(Exception):
    pass


# ============================================================
# PATHS + CORPUS
# ============================================================
def config_dir() -> Path:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(cfg) if cfg else Path.home() / ".claude"


def recall_dir() -> Path:
    return config_dir() / "hooks" / "recall"


def compiled_path(corpus: str) -> Path:
    name = "index_compiled.json" if corpus in ("", "code", "default") else f"index_compiled_{corpus}.json"
    return recall_dir() / name


def index_path(corpus: str) -> Path:
    suffix = "" if corpus in ("", "code", "default") else f"_{corpus}"
    return recall_dir() / f"semantic_index{suffix}.json"


def load_compiled(corpus: str) -> List[Dict]:
    try:
        data = json.loads(compiled_path(corpus).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def embed_text(entry: Dict) -> str:
    """The note essence we embed: name + tags + summary — the same signal recall ranks on."""
    tags = ", ".join(entry.get("tags", []) or [])
    return "\n".join(p for p in (entry.get("name", ""), tags, entry.get("summary", "")) if p)


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# ============================================================
# EMBEDDING (ollama HTTP, batched)
# ============================================================
def ollama_embed(texts: List[str]) -> List[List[float]]:
    """Return one vector per input text. Raises OllamaUnavailable on any connection failure."""
    payload = json.dumps({"model": MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed", data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OllamaUnavailable(str(exc)) from exc
    vecs = out.get("embeddings")
    if not isinstance(vecs, list) or len(vecs) != len(texts):
        raise OllamaUnavailable("unexpected /api/embed response shape")
    return vecs


def normalize(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def embed_batched(texts: List[str]) -> List[List[float]]:
    out: List[List[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        out.extend(ollama_embed(texts[i:i + EMBED_BATCH]))
    return [normalize(v) for v in out]


# ============================================================
# INDEX (incremental — only re-embed notes whose essence changed)
# ============================================================
def load_index(corpus: str) -> Dict[str, Dict]:
    try:
        return json.loads(index_path(corpus).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_index(corpus: str, index: Dict[str, Dict]) -> None:
    p = index_path(corpus)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def refresh_index(corpus: str) -> Dict[str, Dict]:
    """Return an up-to-date {path: {hash, name, summary, vec}} index. Re-embeds only changed/new
    notes, drops removed ones, keeps unchanged vectors. Raises OllamaUnavailable if embedding is
    needed but ollama is down (a fully-cached index still returns fine with no calls)."""
    entries = load_compiled(corpus)
    cache = load_index(corpus)
    fresh: Dict[str, Dict] = {}
    to_embed: List[str] = []
    to_embed_paths: List[str] = []

    for e in entries:
        path = e.get("path")
        if not path:
            continue
        text = embed_text(e)
        h = text_hash(text)
        cached = cache.get(path)
        if cached and cached.get("hash") == h and cached.get("vec"):
            fresh[path] = cached
        else:
            fresh[path] = {"hash": h, "name": e.get("name", ""), "summary": e.get("summary", ""), "vec": None}
            to_embed.append(text)
            to_embed_paths.append(path)

    if to_embed:
        vecs = embed_batched(to_embed)
        for path, vec in zip(to_embed_paths, vecs):
            fresh[path]["vec"] = vec

    if fresh != cache:
        save_index(corpus, fresh)
    return fresh


# ============================================================
# SEARCH
# ============================================================
def cosine_pre_normalized(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both L2-normalized -> dot = cosine


def search(query: str, corpus: str, k: int) -> List[Dict]:
    index = refresh_index(corpus)
    qvec = normalize(ollama_embed([query])[0])
    scored = []
    for path, rec in index.items():
        vec = rec.get("vec")
        if not vec:
            continue
        scored.append((cosine_pre_normalized(qvec, vec), path, rec))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [{"score": s, "path": p, "name": r.get("name", ""), "summary": r.get("summary", "")}
            for s, p, r in scored[:k]]


def first_sentence(summary: str, limit: int = 130) -> str:
    s = " ".join(summary.split())
    return s[:limit].rstrip() + ("…" if len(s) > limit else "")


# ============================================================
# CLI
# ============================================================
def main() -> int:
    # Vault output may be non-ASCII; Windows Python defaults stdout to a locale codepage, which mangles it and
    # crashes on chars like → in summaries. Force UTF-8 so results print (and pipe) correctly.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Semantic (meaning-based) wiki search — Tier-2, on-demand.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("index", help="(re)build the vector index incrementally")
    pi.add_argument("--corpus", default="code")
    ps = sub.add_parser("search", help="cosine top-K by meaning")
    ps.add_argument("query")
    ps.add_argument("-k", type=int, default=DEFAULT_K)
    ps.add_argument("--corpus", default="code")
    args = ap.parse_args()

    try:
        if args.cmd == "index":
            idx = refresh_index(args.corpus)
            vecs = sum(1 for r in idx.values() if r.get("vec"))
            print(f"[semantic] {args.corpus} corpus indexed: {vecs} notes with vectors ({MODEL})")
            return 0

        results = search(args.query, args.corpus, args.k)
        if not results:
            print(f"[semantic] no notes in the {args.corpus} corpus index — is the wiki compiled?")
            return 0
        print(f"[semantic] {MODEL} · query: \"{args.query}\" · top {len(results)}")
        for r in results:
            print(f"  {r['score']:.3f}  {r['path']}  —  {r['name']}: {first_sentence(r['summary'])}")
        return 0
    except OllamaUnavailable as exc:
        print(f"[semantic] ollama/{MODEL} unavailable ({exc}). Falling back to lexical recall — "
              f"start ollama or rely on the [RECALL] hook.", file=sys.stderr)
        print("[semantic] unavailable — use lexical recall for this query.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
