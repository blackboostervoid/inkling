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
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

# Where the embeddings come from. Two shapes are supported and the URL picks between
# them, so there is no provider flag to get wrong:
#   ollama              http://127.0.0.1:11434        -> POST /api/embed
#   OpenAI-compatible   http://127.0.0.1:1234/v1      -> POST /v1/embeddings
# LM Studio, llama.cpp and vLLM all speak the second one. CLAUDE_EMBED_URL wins when set;
# OLLAMA_HOST is still honoured so an existing setup keeps working untouched.
# The default is the IPv4 LITERAL, never the name "localhost": these servers bind IPv4 only, and on
# Windows the name resolves to ::1 first, where a refused loopback connection costs ~2 s before the
# client falls back. Measured 2026-08-06: 2.04 s of pure waiting on every single embed batch, which
# on a full `index` run over a vault is 2 s per 24-note batch and looks exactly like a slow model.
EMBED_URL = os.environ.get("CLAUDE_EMBED_URL", "").rstrip("/") or \
    os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_URL = EMBED_URL  # kept for the existing call sites
MODEL = os.environ.get("CLAUDE_EMBED_MODEL", "bge-m3")
EMBED_BATCH = 24
DEFAULT_K = 8

# A note is only worth proposing as a link when it stands clear of the crowd. Measured on
# a 112-note vault: a correct neighbour sat 0.08 to 0.23 above the fifth-ranked one, while
# noise sat at 0.03. The bar is set inside that gap, and it is a starting point rather than
# a calibration - re-measure on your own vault before trusting it.
SUGGEST_MIN_SCORE = 0.45
SUGGEST_MIN_GAP = 0.06
SUGGEST_MAX_PER_NOTE = 3

# Rerank is OFF unless a model is named. Same discipline as the rest of this layer:
# absent means the previous behaviour, never a failure.
RERANK_MODEL = os.environ.get("CLAUDE_RERANK_MODEL", "").strip()
RERANK_CANDIDATES = 8   # how many cosine hits the judge reads
RERANK_TIMEOUT = 120
# Below this the note is dropped rather than shown. This is the whole point of the second
# stage: cosine ranks but cannot abstain, so it always presents a best answer even when the
# vault holds nothing on the subject. The judge scores each note on its own and can put every
# one of them at zero - which is how "found nothing" becomes sayable.
RERANK_FLOOR = 6.0


class OllamaUnavailable(Exception):
    """The embedding server is not reachable. Named for the common case; raised for any
    provider. Every caller treats it as 'this optional layer is simply off today'."""


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
def is_openai_compatible() -> bool:
    """An /v1 suffix means the OpenAI embeddings shape. LM Studio, llama.cpp and vLLM all
    expose it; ollama does not, and gets its own endpoint."""
    return EMBED_URL.rstrip("/").endswith("/v1")


def ollama_embed(texts: List[str]) -> List[List[float]]:
    """Return one vector per input text, from whichever server EMBED_URL points at.
    Raises OllamaUnavailable on any connection or shape failure, which every caller
    treats as 'the optional layer is off'."""
    if is_openai_compatible():
        url = f"{EMBED_URL}/embeddings"
        payload = json.dumps({"model": MODEL, "input": texts}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        # Some OpenAI-compatible servers demand an Authorization header even when they
        # ignore its value. A dummy key costs nothing and removes a whole class of 401.
        headers["Authorization"] = "Bearer " + os.environ.get("CLAUDE_EMBED_KEY", "not-needed")
    else:
        url = f"{EMBED_URL}/api/embed"
        payload = json.dumps({"model": MODEL, "input": texts}).encode("utf-8")
        headers = {"Content-Type": "application/json"}

    req = urllib.request.Request(url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OllamaUnavailable(f"{url}: {exc}") from exc

    if is_openai_compatible():
        data = out.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise OllamaUnavailable("unexpected /v1/embeddings response shape")
        return [row.get("embedding", []) for row in data]

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


# ============================================================
# RERANK — a local model reads the shortlist and re-scores it
# ============================================================
JUDGE_PROMPT = (
    "You judge whether a note is worth showing to someone who asked a question.\n\n"
    "QUESTION: {query}\n\n"
    "NOTE TITLE: {name}\n"
    "NOTE SUMMARY: {summary}\n\n"
    "How useful is this note for answering that question? Reply with ONE number:\n"
    "0 = unrelated, 3 = same general field but does not address it, "
    "6 = touches the question, 10 = directly answers it.\n"
    "Answer with the number only, no words."
)


def judge_once(query: str, name: str, summary: str) -> Optional[float]:
    """Score one candidate. Returns None when the judge is unreachable or unparseable,
    which the caller treats as 'no opinion' rather than as a zero."""
    prompt = JUDGE_PROMPT.format(query=query, name=name, summary=summary[:600])
    if is_openai_compatible():
        url = f"{EMBED_URL}/chat/completions"
        payload = {"model": RERANK_MODEL, "temperature": 0,
                   "messages": [{"role": "user", "content": prompt}], "max_tokens": 16}
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + os.environ.get("CLAUDE_EMBED_KEY", "not-needed")}
    else:
        url = f"{EMBED_URL}/api/generate"
        # think=False matters more than it looks: a thinking model spends the token
        # budget on reasoning and returns an EMPTY string with done_reason "length",
        # which reads as an unreachable judge rather than as a misconfigured one.
        payload = {"model": RERANK_MODEL, "prompt": prompt, "stream": False, "think": False,
                   "options": {"temperature": 0, "num_predict": 16}}
        headers = {"Content-Type": "application/json"}

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=RERANK_TIMEOUT) as resp:
            out = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None

    if is_openai_compatible():
        text = (out.get("choices") or [{}])[0].get("message", {}).get("content", "")
    else:
        text = out.get("response", "")

    # Tolerant parse: the first number in the reply, however the model dressed it up.
    match = re.search(r"\d+(?:\.\d+)?", str(text))
    if not match:
        return None
    return max(0.0, min(10.0, float(match.group(0))))


def rerank(query: str, results: List[Dict]) -> List[Dict]:
    """Re-score the shortlist with the judge. Candidates the judge had no opinion on
    keep their cosine order below the ones it did score, so a flaky judge degrades to
    the old ranking instead of shuffling it."""
    if not RERANK_MODEL or not results:
        return results
    judged, unjudged = [], []
    for r in results:
        score = judge_once(query, r.get("name", ""), r.get("summary", ""))
        if score is None:
            unjudged.append(r)
        else:
            r["judge"] = score
            judged.append(r)
    judged.sort(key=lambda r: (r["judge"], r["score"]), reverse=True)
    kept = [r for r in judged if r["judge"] >= RERANK_FLOOR]
    if judged and not kept:
        # Every candidate was rejected. Returning the least-bad one would undo the only
        # thing this stage adds, so return nothing and let the caller say so.
        return unjudged
    return kept + unjudged


def first_sentence(summary: str, limit: int = 130) -> str:
    s = " ".join(summary.split())
    return s[:limit].rstrip() + ("…" if len(s) > limit else "")


# ============================================================
# CLI
# ============================================================
# ============================================================
# SUGGEST — propose `related` links, never write them
# ============================================================
def existing_related(entry: Dict, wiki_root: Optional[Path]) -> set:
    """Links the note already declares, so a proposal never repeats one."""
    return {r.strip().lower() for r in entry.get("related", []) if r.strip()}


def neighbours(path: str, index: Dict[str, Dict], limit: int) -> List[Dict]:
    """Nearest notes to the note at `path` by meaning, excluding itself, gap-gated.
    The vector index is keyed by vault-relative path; each record carries name, summary, vec.

    The gap between the best neighbour and the runner-up is what separates a real
    neighbour from a corpus-wide hum: on a heterogeneous vault every note is mildly
    similar to every other, so an absolute score alone proposes noise with confidence."""
    me = index.get(path)
    if not me or not me.get("vec"):
        return []
    scored = []
    for other, rec in index.items():
        if other == path or not rec.get("vec"):
            continue
        scored.append({"name": rec.get("name", other), "score": cosine_pre_normalized(me["vec"], rec["vec"]),
                       "path": other, "summary": rec.get("summary", "")})
    scored.sort(key=lambda r: r["score"], reverse=True)
    if not scored:
        return []

    floor = scored[min(4, len(scored) - 1)]["score"]
    out = []
    for cand in scored[:limit]:
        if cand["score"] < SUGGEST_MIN_SCORE:
            break
        if cand["score"] - floor < SUGGEST_MIN_GAP:
            break
        out.append(cand)
    return out


def suggest(corpus: str, only: Optional[str], limit: int) -> int:
    """Print `related` proposals as ready-to-paste YAML. Writes nothing."""
    index = refresh_index(corpus)
    compiled = {e["path"]: e for e in load_compiled(corpus)}

    if only:
        # Accept a note name, a bare stem or a full path - whichever the caller has.
        wanted = only.strip().lower().removesuffix(".md")
        targets = [key for key, rec in index.items()
                   if key.lower() == only.strip().lower()
                   or str(rec.get("name", "")).lower() == wanted
                   or key.lower().removesuffix(".md").endswith("/" + wanted)]
        if not targets:
            print(f"[semantic] no note matching '{only}' in the {corpus} corpus")
            return 1
    else:
        targets = sorted(index)

    proposed = 0
    for path in targets:
        entry = compiled.get(path, {})
        already = existing_related(entry, None)
        picks = [n for n in neighbours(path, index, limit)
                 if n["path"].lower() not in already and n["name"].lower() not in already]
        if not picks:
            continue
        proposed += 1
        print(f"\n{index[path].get('name', path)}  ({path})")
        print("related:")
        for p in picks:
            print(f"  - {p['path']}    # {p['score']:.3f}  {first_sentence(p['summary'], 90)}")

    if not proposed:
        print(f"[semantic] no proposals: every note either has its links already or has no "
              f"neighbour clearing score {SUGGEST_MIN_SCORE} with a {SUGGEST_MIN_GAP} gap")
        return 0

    print(f"\n[semantic] {proposed} note(s) have proposals above the bar. Nothing was written: "
          f"paste the lines you agree with into each note's frontmatter.")
    return 0


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
    ps.add_argument("--rerank", action="store_true",
                    help="re-score the shortlist with CLAUDE_RERANK_MODEL (slower, sharper)")
    ps.add_argument("--json", action="store_true",
                    help="machine-readable result (used by the MCP server; humans want the default)")
    pg = sub.add_parser("suggest", help="propose `related` links for one note or the whole vault")
    pg.add_argument("--note", default=None, help="note name (filename stem); omit for every note")
    pg.add_argument("-k", type=int, default=SUGGEST_MAX_PER_NOTE)
    pg.add_argument("--corpus", default="code")
    args = ap.parse_args()

    try:
        if args.cmd == "index":
            idx = refresh_index(args.corpus)
            vecs = sum(1 for r in idx.values() if r.get("vec"))
            print(f"[semantic] {args.corpus} corpus indexed: {vecs} notes with vectors ({MODEL})")
            return 0

        if args.cmd == "suggest":
            return suggest(args.corpus, args.note, args.k)

        wanted = args.k
        if getattr(args, "rerank", False) and RERANK_MODEL:
            # Read a wider shortlist so the judge has something to choose between.
            results = search(args.query, args.corpus, max(RERANK_CANDIDATES, wanted))
            results = rerank(args.query, results)[:wanted]
        else:
            results = search(args.query, args.corpus, wanted)

        wants_json = getattr(args, "json", False)
        reranked = bool(getattr(args, "rerank", False)) and bool(RERANK_MODEL)
        if not results:
            # THE VERDICT MUST SURVIVE THE FORMAT. "The judge read every candidate and rejected all
            # of them" and "there is no index to search" are opposite facts, and an empty list says
            # neither. The prose branch already distinguished them; the JSON branch has to as well,
            # or a caller reading the machine format loses the one thing this stage adds.
            if reranked:
                verdict, note = "rejected", (
                    f"nothing in the vault answers this — every candidate scored below "
                    f"{RERANK_FLOOR:.0f}/10 with {RERANK_MODEL}")
            else:
                verdict, note = "no-index", (
                    f"no notes in the {args.corpus} corpus index — is the wiki compiled?")
            if wants_json:
                print(json.dumps({"query": args.query, "corpus": args.corpus, "model": MODEL,
                                  "judge": RERANK_MODEL if reranked else None,
                                  "verdict": verdict, "note": note, "results": []},
                                 ensure_ascii=False))
            else:
                print(f"[semantic] {MODEL}{' + ' + RERANK_MODEL if reranked else ''} · {note}")
            return 0
        if wants_json:
            print(json.dumps({"query": args.query, "corpus": args.corpus, "model": MODEL,
                              "judge": RERANK_MODEL if reranked else None,
                              "verdict": "hits", "note": None, "results": results},
                             ensure_ascii=False))
            return 0
        judged = " + " + RERANK_MODEL if any("judge" in r for r in results) else ""
        print(f"[semantic] {MODEL}{judged} · query: \"{args.query}\" · top {len(results)}")
        for r in results:
            mark = f"  judge {r['judge']:.0f}/10" if "judge" in r else ""
            print(f"  {r['score']:.3f}{mark}  {r['path']}  —  {r['name']}: {first_sentence(r['summary'])}")
        return 0
    except OllamaUnavailable as exc:
        if getattr(args, "json", False):
            # Unreachable is NOT the same as empty, and a caller that cannot tell them apart will
            # report "the vault has nothing on this" when the embedder is simply not running.
            print(json.dumps({"query": getattr(args, "query", ""), "corpus": args.corpus,
                              "model": MODEL, "judge": None, "verdict": "unavailable",
                              "note": f"{MODEL} unavailable ({exc}) - fall back to lexical recall",
                              "results": []}, ensure_ascii=False))
            return 0
        print(f"[semantic] ollama/{MODEL} unavailable ({exc}). Falling back to lexical recall — "
              f"start ollama or rely on the [RECALL] hook.", file=sys.stderr)
        print("[semantic] unavailable — use lexical recall for this query.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
