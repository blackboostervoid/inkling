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

  python3 semantic.py index                 # (re)build the vector index incrementally
  python3 semantic.py search "<query>" [-k N]   # refresh, then cosine top-K

Engine: ollama + bge-m3 (1024-dim, multilingual, which a vault in any language needs). Since
2026-08-06 it embeds the note BODY at H2-section granularity, plus the name+tags+summary essence
as chunk zero — the two rails were reading identical fields until then, which made the second one
a duplicate rather than a complement. Matching happens on sections; results are whole NOTES with
the matched section headings named. Vectors are L2-normalized and cached in semantic_index.json
keyed by path + a hash over every chunk, so editing a body now invalidates that note (the old
frontmatter-only hash did not). ollama down / model missing -> graceful message, exit 0, fall back
to lexical.

Stdlib only. Invoke via `py`.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import math
import os
import re
import socket
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
EMBED_BATCH = 24
VOYAGE_BATCH = 128
DEFAULT_K = 8

# ------------------------------------------------------------------
# DUAL BACKEND — the same shape the code index has carried since it gained a second machine
# ------------------------------------------------------------------
# The desktop runs ollama; the laptop is ARM64 with no local models at all, and there the wiki's
# whole PULL door was dead — not degraded, dead, because an embedder has no honest fallback the way
# the judge does. The code index solved this already: declare the
# backends, pick the preferred AVAILABLE one, and let the INDEX carry which model built it.
#
# The cloud model here is `voyage-3`, not the code index's `voyage-code-3`: this corpus is prose in
# two languages, not source. Both are 1024-dim, so the index shape does not change with the backend.
#
# CROSS-MODEL HAZARD, and how it is made impossible rather than guarded: vectors from two models are
# not comparable, and comparing them yields a confident wrong ranking rather than an error. So the
# MODEL IS PART OF THE INDEX FILENAME. A machine with a different backend writes a different file
# and can never read the other one by accident. No meta field to check, no migration to remember.
#
# THIS REGISTRY IS THE ONLY PLACE A MODEL IS NAMED, and that is what closes the last way in. There
# used to be a `MODEL = os.environ.get("CLAUDE_EMBED_MODEL", "bge-m3")` beside EMBED_URL, and
# `ollama_embed` put it in the request body while the DISPATCH picked a model from this table. The
# two agreed only because ollama had exactly one entry here. Declare a second and the env var sends
# ollama a different model than the one the index filename records — the guard above, defeated
# through the one door it does not watch, by a variable the published README tells people to set.
# The rule is already written in embed_with's docstring: the model is the key, never a global flag.
# Choosing a different local model means adding it HERE; an env var cannot reach past this table.
BACKENDS = {
    "bge-m3":   {"kind": "ollama", "dim": 1024},
    "voyage-3": {"kind": "voyage", "dim": 1024},
}
PREFERRED_ORDER = ["bge-m3", "voyage-3"]   # local and free first, cloud second
_AVAIL_CACHE: Dict[str, bool] = {}

# A note is only worth proposing as a link when it stands clear of the crowd. Measured on
# a 112-note vault: a correct neighbour sat 0.08 to 0.23 above the fifth-ranked one, while
# noise sat at 0.03. The bar is set inside that gap, and it is a starting point rather than
# a calibration - re-measure on your own vault before trusting it.
SUGGEST_MIN_SCORE = 0.45
SUGGEST_MIN_GAP = 0.06
SUGGEST_MAX_PER_NOTE = 3

# ------------------------------------------------------------------
# BODY CHUNKING — what separates this rail from the lexical one
# ------------------------------------------------------------------
# The push rail (recall_scanner) ranks notes on name + tags + summary, and until now this
# rail embedded exactly the same three fields. Two doors reading the same three fields is
# one memory with two handles, not two memories. So the split is by WHAT is read: the push
# rail asks "is there a note ABOUT this", this one asks "did anyone WRITE anything that
# touches this". That second question needs the body, which nothing has ever indexed.
#
# Granularity is the H2 section, measured rather than picked: across 116 notes there are 877
# `## ` sections, 7.6 per note, and not one note lacks them. A section is also the unit a
# human wrote as one thought, which is what makes it survive being retrieved alone.
MAX_CHUNK_CHARS = 1200

# Sections excluded from the body index, by heading.
#
# `Применялось` and `Найдено поиском` are the usage ledgers recall_evidence.py appends, one per
# door — machine-written lines carrying session ids and the agent's own reasons for applying a
# note. Indexing either would let the rail retrieve notes on the strength of its own past
# retrievals, the self-poisoning loop capture already had to close. `Найдено поиском` is the more
# dangerous of the two and the reason this comment now names both: it records what THIS rail
# returned, so indexing it closes the loop on itself in one hop instead of two. Any future
# ledger section belongs on this list the same day it is created — the guard is keyed to a
# literal heading, so a new heading is silently un-guarded until someone adds it.
#
# The rest are NAVIGATION: lists of links and citations that point at content instead of being
# it, so a hit inside one is a hit on a signpost.
#
# Matched EXACTLY, never as a substring, and the reason is a live near-miss in this vault:
# `Источники` is a citation list, while `Где источники расходятся` is where a research note says
# its sources CONFLICT — the most substantive section such a note has. A substring rule on
# "источник" would take out the second along with the first.
# The evidence sections recall_evidence.py writes must never be indexed: indexing them would let
# the body search find a note on the strength of its OWN past hits, a self-poisoning loop closed in
# one hop. Keep these in step with EVIDENCE_HEADER and SMRITI_HEADER over there.
SKIP_HEADINGS = frozenset((
    "applied", "found by search",
    "sources", "see also", "links",
))
# The one prefix rule, for a family that only ever appears as a link list: "Связанные проекты",
# "Связанные идеи", "Связанные темы", each usually behind a 🔗.
SKIP_HEADING_PREFIXES = ("связанные ",)
_HEADING_TRIM_RE = re.compile(r"^[^\wЀ-ӿ]+|[^\wЀ-ӿ]+$")


def is_navigational(heading: str) -> bool:
    """True for a heading that introduces pointers rather than content. Leading emoji and
    punctuation are stripped first — `🔗 Связанные темы` is the same section as `Связанные темы`."""
    h = _HEADING_TRIM_RE.sub("", heading).strip().lower()
    return h in SKIP_HEADINGS or h.startswith(SKIP_HEADING_PREFIXES)

# Notes whose body could not be read during this process's indexing pass. Collected rather than
# swallowed so `index` can report the count: essence-only is a valid degradation, but an
# UNANNOUNCED one reads as a full index and is how a corpus stays half-blind for months.
UNREADABLE: List[str] = []

# Rerank candidates that scored nothing not because the judge looked and abstained, but because
# it never answered in time or could not be reached at all. Same discipline as UNREADABLE: a
# judge that timed out on every candidate looked exactly like a judge that read every candidate
# and liked none of them, and that distinction has to survive to the person reading the results.
RERANK_ABANDONED: List[str] = []
RERANK_UNREACHABLE: List[str] = []
RERANK_ANSWERED: List[str] = []

# Rerank is OFF unless a model is named. Same discipline as the rest of this layer:
# absent means the previous behaviour, never a failure.
RERANK_MODEL = os.environ.get("CLAUDE_RERANK_MODEL", "").strip()
RERANK_CANDIDATES = 12  # how many notes the judge reads (was 8; the body index surfaces more)
RERANK_WORKERS = 4      # judge calls run concurrently; each candidate still gets its OWN call

# TWO CLOCKS, and conflating them is what put a 120 s deadline inside a 60 s hook.
#
# Under a hook, this file has exactly as long as Claude Code gave the hook; overrun means the
# whole SessionStart entry is cancelled and its output discarded, so the index silently is not
# rebuilt. Run by hand or through the vault-search MCP there is no hook clock at all, and
# cutting a person's own search short to satisfy a budget nobody is enforcing would be worse
# than slow.
INTERACTIVE_DEADLINE_S = 900


def _deadline_s() -> float:
    """Seconds this call may take: the hook's budget when under one, generous when not.

    CLAUDE_HOOK_BUDGET_KEY is set by whichever hook command invokes this file, and its absence
    is the honest signal that nothing is holding a stopwatch.
    """
    key = os.environ.get("CLAUDE_HOOK_BUDGET_KEY", "").strip()
    if not key:
        return float(INTERACTIVE_DEADLINE_S)
    sys.path.insert(0, str(Path(os.environ["CLAUDE_CONFIG_DIR"]) / "scripts"))
    from hook_budget import budget_for  # noqa: E402  - resolved from the config dir at runtime
    return budget_for(key) / 1000.0

# Every candidate's evidence is truncated to the same budget before the judge sees it. An LLM
# judge has a documented length bias — a longer passage reads as a better answer — and body
# sections vary from two lines to two pages, so unequal lengths would score the layout rather
# than the content. Equal budgets make the comparison about substance.
JUDGE_EVIDENCE_CHARS = 900
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


def index_path(corpus: str, model: str) -> Path:
    """One index file per (corpus, model). The model in the NAME is what makes cross-model
    contamination impossible instead of merely detected — see the CROSS-MODEL HAZARD note."""
    suffix = "" if corpus in ("", "code", "default") else f"_{corpus}"
    return recall_dir() / f"semantic_index{suffix}__{model}.json"


def existing_index_models(corpus: str) -> List[str]:
    """Models that already have an index for this corpus, preferred order first."""
    return [m for m in PREFERRED_ORDER if index_path(corpus, m).is_file()]


# ============================================================
# BACKEND RESOLUTION (resolved per call; cheap, and the answer can change between machines)
# ============================================================
def voyage_key() -> Optional[str]:
    """From `$CLAUDE_CONFIG_DIR/tokens/voyage.env`, via the one reader every secret goes through.

    This used to consult the environment first and then a `.env` beside the config dir. Both
    paths are gone: the environment variable silently outranked the file and is invisible when
    you go looking, which is why replacing the key twice appeared to change nothing.
    """
    scripts_dir = str(config_dir() / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from secrets_reader import voyage_key as _read  # noqa: E402  (path set just above)
    return _read()


def ollama_has_model(model: str) -> bool:
    try:
        with urllib.request.urlopen(f"{EMBED_URL}/api/tags", timeout=3) as resp:
            tags = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False
    names = {str(m.get("name", "")).split(":")[0] for m in tags.get("models", [])}
    return model in names


def backend_available(model: str) -> bool:
    if model in _AVAIL_CACHE:
        return _AVAIL_CACHE[model]
    kind = BACKENDS.get(model, {}).get("kind")
    if kind == "ollama":
        ok = ollama_has_model(model)
    elif kind == "voyage":
        ok = voyage_key() is not None
    else:
        ok = False
    _AVAIL_CACHE[model] = ok
    return ok


def resolve_build_model() -> Optional[str]:
    """The preferred backend this machine can actually serve."""
    return next((m for m in PREFERRED_ORDER if backend_available(m)), None)


def resolve_query_model(corpus: str) -> Optional[str]:
    """The model of an existing index this machine can serve, or None.

    An index built elsewhere with a backend this machine lacks is NOT an error to paper over: the
    honest answer is that the search cannot run here, not a silently worse ranking."""
    return next((m for m in existing_index_models(corpus) if backend_available(m)), None)


def effective_model(corpus: str) -> str:
    """The model that will actually answer for `corpus` here: an existing index this machine can
    serve, else the best backend it could build one with. Empty string when neither exists.

    EVERY REPORT OF WHICH MODEL ANSWERED MUST COME FROM HERE. Until 2026-08-07 those lines printed
    a module constant defaulting to `bge-m3`, which named a preference rather than a fact and so
    labelled every Voyage answer `bge-m3` on any machine without ollama. The CROSS-MODEL HAZARD note
    above makes mixing two models' vectors impossible by putting the model in the index FILENAME;
    the printed name is the only way a human ever SEES which one was used, so a name that cannot be
    wrong is worth more here than one that is merely usually right."""
    return resolve_query_model(corpus) or resolve_build_model() or ""


def load_compiled(corpus: str) -> List[Dict]:
    try:
        data = json.loads(compiled_path(corpus).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def embed_text(entry: Dict) -> str:
    """The note essence: name + tags + summary — the same signal the push rail ranks on.
    Still indexed, as chunk zero, so a query that matches the LABEL keeps working."""
    tags = ", ".join(entry.get("tags", []) or [])
    return "\n".join(p for p in (entry.get("name", ""), tags, entry.get("summary", "")) if p)


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def corpus_config(corpus: str) -> Dict:
    """Per-namespace settings from corpora.json — the registry compile_wiki_index.py, main.go and
    recall_evidence.py already read. The default corpus has no entry and never needs one."""
    if corpus in ("", "code", "default"):
        return {}
    try:
        registry = json.loads((recall_dir() / "corpora.json").read_text(encoding="utf-8"))
        entry = registry.get(corpus)
        return entry if isinstance(entry, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def body_indexing_enabled(corpus: str) -> bool:
    """Whether this corpus gets its BODY embedded, or only its label. Opt-out per namespace via
    `"body": false`, for a vault where fine-grained retrieval buys nothing."""
    return corpus_config(corpus).get("body", True) is not False


def _same_or_inside(child: Path, parent: Path) -> bool:
    """Windows path containment, case- and separator-insensitive. `Path.resolve()` does not
    normalise case here, and a drive letter arrives both ways, so the comparison is done on
    normalised strings rather than on Path equality."""
    def norm(p: Path) -> str:
        return os.path.normcase(os.path.normpath(str(p))).rstrip("\\/")
    c, p = norm(child), norm(parent)
    return c == p or c.startswith(p + os.sep)


def corpus_out_of_scope(corpus: str) -> Optional[str]:
    """Return a refusal reason when this corpus must not answer from the current directory.

    A namespace may declare `"project"` in corpora.json, which binds it to one working tree. The
    standing case is a personal vault kept for the assistant, which has no business surfacing
    while the work is code. The push rail is already bound this way — only that project's lens
    sets its `wiki_index` — but the pull door takes the corpus as an ARGUMENT,
    so from any other project it could simply be asked for. This is that missing half.

    Scoped by CURRENT DIRECTORY, and that is sound because the MCP server inherits the session's
    project directory. Probed rather than assumed: the server reported the session's own project
    path, spelled absolutely, while that session ran there.

    `project` accepts a LIST as well as a single path, because one project can sit at two
    absolute paths at once: a folder synced between two machines gets a different drive letter
    or mount point on each.
    A single string made the binding refuse everywhere on the second machine — the corpus was
    not merely unreachable there, it explained its own absence as deliberate scoping (found
    2026-08-07 during laptop bring-up). Matching ANY declared path is what "bound to this
    project" means; the paths are one tree seen through two drive letters, not two trees.

    Returns None when the corpus is free to answer.
    """
    declared = corpus_config(corpus).get("project") or []
    paths = [declared] if isinstance(declared, str) else list(declared)
    paths = [str(p).strip() for p in paths if str(p).strip()]
    if not paths:
        return None
    if any(_same_or_inside(Path.cwd(), Path(p)) for p in paths):
        return None
    return (f"the '{corpus}' corpus is bound to {' or '.join(paths)} and this session is running "
            f"in {Path.cwd()}. It is scoped on purpose, not missing — open that project to search it.")


def wiki_root(corpus: str = "code") -> Optional[Path]:
    """Vault root for a corpus. The default one comes from CLAUDE_WIKI_PATH, exactly as
    compile_wiki_index.py requires; a NAMESPACE gets its own root from corpora.json, which is
    where compile_wiki_index.py, main.go and recall_evidence.py already read theirs.

    Reading the env var for every corpus is what a first pass did, and it silently half-worked:
    25 of 71 notes in a second corpus are stored relative to their own vault while the rest
    resolve as `../personal/...` from the code vault, so a third of it indexed its label and none
    of its body, with nothing said. Unset means essence-only — a degradation, not a failure.
    """
    declared = corpus_config(corpus).get("wiki_path", "").strip()
    if declared:
        return Path(declared)
    raw = os.environ.get("CLAUDE_WIKI_PATH", "").strip()
    return Path(raw) if raw else None


_H2_SPLIT_RE = re.compile(r"(?m)^##\s+(?P<heading>.+?)\s*$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)


def split_long(heading: str, text: str) -> List[Dict]:
    """A section past the budget is split on blank lines, never mid-sentence. Each piece keeps
    its parent heading so it can still be reported as part of that section."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [{"heading": heading, "text": text}]
    pieces, current = [], ""
    for para in text.split("\n\n"):
        if current and len(current) + len(para) + 2 > MAX_CHUNK_CHARS:
            pieces.append({"heading": heading, "text": current.strip()})
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current.strip():
        pieces.append({"heading": heading, "text": current.strip()})
    return pieces


def note_chunks(entry: Dict, root: Optional[Path]) -> List[Dict]:
    """The units embedded for one note: chunk zero is the essence (name+tags+summary), then one
    per H2 section of the body.

    Chunk zero is kept deliberately. Dropping it would trade one blindness for another — a note
    whose body never states its own subject would stop being findable by that subject.
    """
    chunks = [{"heading": "", "text": embed_text(entry)}]
    path = entry.get("path")
    if not root or not path:
        return chunks
    try:
        raw = (root / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        # A body that cannot be read is a note indexed at half strength, and the caller has to be
        # able to say so out loud. Silence here is what let a third of one corpus go body-blind.
        UNREADABLE.append(str(path))
        return chunks

    body = _FRONTMATTER_RE.sub("", raw, count=1)
    marks = list(_H2_SPLIT_RE.finditer(body))
    name = entry.get("name", "")
    for i, m in enumerate(marks):
        heading = m.group("heading").strip()
        if is_navigational(heading):
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        section = body[m.end():end].strip()
        if not section:
            continue
        for piece in split_long(heading, section):
            # The note name rides along in the embedded text: a bare section says "three
            # sentences about caching" without ever saying which subsystem's caching, and a
            # vector built from that drifts toward every other note that mentions caching.
            piece["embed"] = f"{name}\n{heading}\n{piece['text']}"
            chunks.append(piece)
    return chunks


def chunk_embed_text(chunk: Dict) -> str:
    return chunk.get("embed") or chunk["text"]


def note_hash(chunks: List[Dict]) -> str:
    """One hash over every chunk, so editing the BODY invalidates the note's vectors. The old
    hash covered the frontmatter alone, which is precisely why a body edit changed nothing."""
    return text_hash(chr(10).join(chunk_embed_text(c) for c in chunks))


# ============================================================
# EMBEDDING (ollama HTTP, batched)
# ============================================================
def is_openai_compatible() -> bool:
    """An /v1 suffix means the OpenAI embeddings shape. LM Studio, llama.cpp and vLLM all
    expose it; ollama does not, and gets its own endpoint."""
    return EMBED_URL.rstrip("/").endswith("/v1")


def voyage_embed(texts: List[str], model: str, input_type: str) -> List[List[float]]:
    """Cloud embedder — the only path on a machine that cannot run a local model.

    `input_type` matters here and does not for bge-m3: Voyage embeds a QUERY and a DOCUMENT into
    different spaces on purpose, and passing the wrong one costs recall silently.

    Only transport and response-shape failures are converted to "the layer is off". Anything else
    propagates, because an unexpected exception from the client is a bug in this code or in the
    package, and reporting a bug as a missing optional layer is how it stays unfixed for months.
    """
    key = voyage_key()
    if not key:
        raise OllamaUnavailable(
            "VOYAGE_API_KEY is not set: expected a line in $CLAUDE_CONFIG_DIR/tokens/voyage.env")
    try:
        import voyageai
    except ImportError as exc:
        raise OllamaUnavailable("the `voyageai` package is not installed "
                                "(python3 -m pip install voyageai)") from exc
    out: List[List[float]] = []
    try:
        client = voyageai.Client(api_key=key)
        for i in range(0, len(texts), VOYAGE_BATCH):
            batch = texts[i:i + VOYAGE_BATCH]
            out.extend(client.embed(batch, model=model, input_type=input_type).embeddings)
    except (OSError, TimeoutError, ValueError, KeyError, AttributeError) as exc:
        raise OllamaUnavailable(f"voyage embed failed ({type(exc).__name__}: {exc})") from exc
    return out


def embed_with(texts: List[str], model: str, input_type: str = "document") -> List[List[float]]:
    """Dispatch to the backend that owns `model`. The model is the key, never a global flag."""
    kind = BACKENDS.get(model, {}).get("kind")
    if kind == "ollama":
        return ollama_embed(texts, model)
    if kind == "voyage":
        return voyage_embed(texts, model, input_type)
    raise OllamaUnavailable(f"no embedder is declared for model '{model}'")


def ollama_embed(texts: List[str], model: str) -> List[List[float]]:
    """Return one vector per input text, from whichever server EMBED_URL points at.
    Raises OllamaUnavailable on any connection or shape failure, which every caller
    treats as 'the optional layer is off'.

    `model` is the one the dispatch chose, and it is the same name the index filename carries.
    Reading a module-level default here instead is how a request could ask the server for one
    model while the vectors got filed under another."""
    if is_openai_compatible():
        url = f"{EMBED_URL}/embeddings"
        payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        # Some OpenAI-compatible servers demand an Authorization header even when they
        # ignore its value. A dummy key costs nothing and removes a whole class of 401.
        headers["Authorization"] = "Bearer " + os.environ.get("CLAUDE_EMBED_KEY", "not-needed")
    else:
        url = f"{EMBED_URL}/api/embed"
        payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
        headers = {"Content-Type": "application/json"}

    req = urllib.request.Request(url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_deadline_s()) as resp:
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


def embed_batched(texts: List[str], model: str, input_type: str = "document") -> List[List[float]]:
    """`model` is required on purpose. It used to default to the empty string and fall back to a
    module constant, which meant a caller that had failed to resolve a model still got vectors —
    from whichever model the constant happened to name, filed under whatever the caller believed."""
    if not model:
        raise OllamaUnavailable("embed_batched called without a model: the caller must resolve one "
                                "(see effective_model) so the vectors match the index they land in")
    out: List[List[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        out.extend(embed_with(texts[i:i + EMBED_BATCH], model, input_type))
    return [normalize(v) for v in out]


# ============================================================
# INDEX (incremental — only re-embed notes whose essence changed)
# ============================================================
def load_index(corpus: str, model: str) -> Dict[str, Dict]:
    try:
        return json.loads(index_path(corpus, model).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_index(corpus: str, index: Dict[str, Dict], model: str) -> None:
    p = index_path(corpus, model)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def refresh_index(corpus: str, model: str = "") -> Dict[str, Dict]:
    """Return an up-to-date {path: {hash, name, summary, chunks: [{heading, text, vec}]}} index.

    `model` names the backend that owns this index. Empty means "resolve it": an index this machine
    can already serve, else the preferred available backend for a fresh build. Refuses rather than
    guessing when the machine can serve none — an embedder has no honest degraded mode, and a search
    that silently returns nothing is indistinguishable from a vault with nothing in it.

    Re-embeds only notes whose content changed, drops removed ones, keeps unchanged vectors.
    Raises OllamaUnavailable if embedding is needed but the backend is down (a fully-cached index
    still returns fine with no calls).

    The record shape changed when the body was added, and an index written by the previous
    version has no `chunks` key. Rather than migrate it, such a record simply misses the cache
    test and is re-embedded — a one-off full pass over the vault, which is honest work rather
    than a guess about what the old vectors covered.
    """
    model = model or effective_model(corpus)
    if not model:
        raise OllamaUnavailable(
            "no embedding backend on this machine: ollama has no `bge-m3`, and VOYAGE_API_KEY is "
            "not set for the cloud fallback")
    entries = load_compiled(corpus)
    cache = load_index(corpus, model)
    root = wiki_root(corpus) if body_indexing_enabled(corpus) else None
    fresh: Dict[str, Dict] = {}
    pending: List[tuple] = []  # (path, chunk_index, text)

    for e in entries:
        path = e.get("path")
        if not path:
            continue
        chunks = note_chunks(e, root)
        h = note_hash(chunks)
        cached = cache.get(path)
        if cached and cached.get("hash") == h and cached.get("chunks") \
                and all(c.get("vec") for c in cached["chunks"]):
            fresh[path] = cached
            continue
        fresh[path] = {
            "hash": h,
            "name": e.get("name", ""),
            "summary": e.get("summary", ""),
            "chunks": [{"heading": c["heading"], "text": c["text"], "vec": None} for c in chunks],
        }
        for i, c in enumerate(chunks):
            pending.append((path, i, chunk_embed_text(c)))

    if pending:
        vecs = embed_batched([t for _, _, t in pending], model, "document")
        for (path, i, _), vec in zip(pending, vecs):
            fresh[path]["chunks"][i]["vec"] = vec

    if fresh != cache:
        save_index(corpus, fresh, model)
    return fresh


def summary_vec(rec: Dict) -> Optional[List[float]]:
    """The note-level vector: chunk zero, the essence. Neighbour proposals are a statement
    about what a note IS, so they stay on the label — a body-section match would propose a
    link on the strength of one shared paragraph."""
    for c in rec.get("chunks", []):
        if not c.get("heading"):
            return c.get("vec")
    return None


# ============================================================
# SEARCH
# ============================================================
def cosine_pre_normalized(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both L2-normalized -> dot = cosine


# A section is reported as "matched" when it lands within this much of the note's best section.
# Without a band, a note with 45 sections would list all 45; with one, it lists the two or three
# that actually pulled it in. Reporting granularity only — it never affects ranking.
MATCH_BAND = 0.04


def search(query: str, corpus: str, k: int) -> List[Dict]:
    """Match at SECTION granularity, return whole NOTES.

    The two halves are deliberately different. Matching on sections is the entire point — it is
    what lets a question find the paragraph that touches it inside a note about something else.
    Returning notes is what keeps the result usable: a stray paragraph can read correctly inside
    its note and mislead alone, six sections of one note would swallow the whole result list,
    and every ledger in this harness is keyed by note name, so a fragment-shaped result would
    have no place to be counted. Fine matching, coarse delivery.
    """
    model = effective_model(corpus)
    if not model:
        raise OllamaUnavailable(
            "no embedding backend on this machine: ollama has no `bge-m3`, and VOYAGE_API_KEY is "
            "not set for the cloud fallback")
    index = refresh_index(corpus, model)
    # `query`, not `document`: Voyage embeds the two into different spaces on purpose, and using
    # the wrong one costs recall silently. bge-m3 is symmetric and ignores the distinction.
    qvec = normalize(embed_with([query], model, "query")[0])
    scored = []
    for path, rec in index.items():
        chunks = [c for c in rec.get("chunks", []) if c.get("vec")]
        if not chunks:
            continue
        hits = sorted(((cosine_pre_normalized(qvec, c["vec"]), c) for c in chunks),
                      key=lambda t: t[0], reverse=True)
        best_score, best_chunk = hits[0]
        matched = [c["heading"] for s, c in hits
                   if c["heading"] and best_score - s <= MATCH_BAND]
        scored.append({
            "score": best_score,
            "path": path,
            "name": rec.get("name", ""),
            "summary": rec.get("summary", ""),
            "matched": matched,
            # What the judge reads. The essence chunk carries no heading, so a label-match falls
            # back to the summary and behaves exactly as it did before the body existed.
            "evidence": best_chunk["text"] if best_chunk.get("heading") else rec.get("summary", ""),
            "matched_in": "body" if best_chunk.get("heading") else "summary",
        })
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:k]


# ============================================================
# RERANK — a local model reads the shortlist and re-scores it
# ============================================================
# The judge is shown the passage that actually matched, not the note's summary.
#
# This was a live defect the moment the body was indexed, and it would have quietly cancelled
# the whole change: body search exists precisely for the note whose SUMMARY is about something
# else, and a judge reading only that summary would have scored exactly those notes at zero.
# The stage built to add abstention would have been abstaining from its own best finds.
JUDGE_PROMPT = (
    "You judge whether a note is worth showing to someone who asked a question.\n\n"
    "QUESTION: {query}\n\n"
    "NOTE TITLE: {name}\n"
    "{label}: {evidence}\n\n"
    "How useful is this note for answering that question? Reply with ONE number:\n"
    "0 = unrelated, 3 = same general field but does not address it, "
    "6 = touches the question, 10 = directly answers it.\n"
    "Judge the note by this passage. A passage that answers the question makes the note "
    "useful even when the note is filed under another subject.\n"
    "Answer with the number only, no words."
)


def judge_once(query: str, name: str, evidence: str, heading: str = "") -> Optional[float]:
    """Score one candidate on the passage that matched it. Returns None when the judge is
    unreachable or unparseable, which the caller treats as 'no opinion' rather than a zero.

    One call per candidate, never a list. A judge shown several at once systematically favours
    whichever it saw first, and would reproduce the very cosine ordering this stage exists to
    overrule."""
    label = f'SECTION "{heading}"' if heading else "NOTE SUMMARY"
    prompt = JUDGE_PROMPT.format(query=query, name=name, label=label,
                                 evidence=evidence[:JUDGE_EVIDENCE_CHARS])
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
        with urllib.request.urlopen(req, timeout=_deadline_s()) as resp:
            out = json.load(resp)
    except socket.timeout:
        # Collected, not swallowed — same shape as UNREADABLE above, and for the same reason.
        # A rerank that ran out of time and a rerank that had no opinion both scored nothing,
        # so a judge that never once answered inside its budget looked exactly like a judge
        # that read every candidate and liked none of them.
        RERANK_ABANDONED.append(name)
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if isinstance(getattr(exc, "reason", None), socket.timeout):
            RERANK_ABANDONED.append(name)
        else:
            RERANK_UNREACHABLE.append(name)
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
    the old ranking instead of shuffling it.

    Calls run concurrently because each one is independent and the serial cost is what made
    this stage unusable: eight candidates against a 26B judge measured ~34 s. Concurrency
    changes only the wall clock — every candidate still gets its own isolated call, so the
    position-bias guard the separate calls exist for is untouched."""
    if not RERANK_MODEL or not results:
        return results

    def score_of(r: Dict) -> Optional[float]:
        first = (r.get("matched") or [""])[0] if r.get("matched_in") == "body" else ""
        return judge_once(query, r.get("name", ""),
                          r.get("evidence") or r.get("summary", ""), first)

    with futures.ThreadPoolExecutor(max_workers=RERANK_WORKERS) as pool:
        scores = list(pool.map(score_of, results))

    judged, unjudged = [], []
    for r, score in zip(results, scores):
        if score is None:
            unjudged.append(r)
        else:
            r["judge"] = score
            judged.append(r)
    if judged:
        RERANK_ANSWERED.append(RERANK_MODEL)
    judged.sort(key=lambda r: (r["judge"], r["score"]), reverse=True)
    kept = [r for r in judged if r["judge"] >= RERANK_FLOOR]
    if judged and not kept:
        # Every candidate was rejected. Returning the least-bad one would undo the only
        # thing this stage adds, so return nothing and let the caller say so.
        return unjudged
    return kept + unjudged


def rerank_note(answered: Optional[str]) -> Optional[str]:
    if answered:
        return None
    return f"{RERANK_MODEL} did not answer" if RERANK_MODEL else "no CLAUDE_RERANK_MODEL is configured"


def first_sentence(summary: str, limit: int = 130) -> str:
    s = " ".join(summary.split())
    return s[:limit].rstrip() + ("…" if len(s) > limit else "")


# ============================================================
# FIRE LOG — what this door actually handed over
# ============================================================
# The push rail has logged its firings since it was built, which is the only reason anything is
# known about which notes earn their place. This door has been uncounted, so a note that works
# here has looked dead in the ledger — and a weight computed from one door of two would demote
# exactly the notes that work through the other.
#
# One rolling file rather than one per session: nothing in this process knows a session id (the
# MCP server does not receive one, and the CLI has none at all). recall_feedback.py attributes
# lines to a session by TIMESTAMP WINDOW at Stop, which is why the ts is written first and in
# UTC. Only what was RETURNED is logged — the same rule the push rail follows, because a note
# the judge rejected was never handed to anyone.
SMRITI_FIRES = "smriti_fires.jsonl"


def log_fires(results: List[Dict], corpus: str) -> None:
    """Append the returned notes to the rolling fire log. Best-effort: a search must never fail
    because bookkeeping could not be written."""
    if not results:
        return
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        with (recall_dir() / SMRITI_FIRES).open("a", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps({"ts": stamp, "name": r.get("name", ""),
                                     "path": r.get("path", ""), "corpus": corpus},
                                    ensure_ascii=False) + "\n")
    except OSError:
        pass


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
    mine = summary_vec(index.get(path) or {})
    if not mine:
        return []
    scored = []
    for other, rec in index.items():
        other_vec = summary_vec(rec)
        if other == path or not other_vec:
            continue
        scored.append({"name": rec.get("name", other), "score": cosine_pre_normalized(mine, other_vec),
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
    # Resolved once, and every line below that NAMES a model reads this — including the failure
    # branches, where the honest answer is the empty string rather than a default that was never
    # consulted. See effective_model for why a resolved name beats a declared default here.
    model = effective_model(getattr(args, "corpus", "code"))

    try:
        if args.cmd == "index":
            idx = refresh_index(args.corpus, model)
            notes = sum(1 for r in idx.values() if any(c.get("vec") for c in r.get("chunks", [])))
            chunks = sum(1 for r in idx.values() for c in r.get("chunks", []) if c.get("vec"))
            body = chunks - notes  # chunk zero per note is the essence; the rest is body
            # Three states, never two. "Deliberately label-only" and "could not find the vault"
            # produce the same index and must not print the same sentence — the identical
            # collapse that makes a broken-server signal unreadable on a healthy server.
            root = wiki_root(args.corpus)
            if not body_indexing_enabled(args.corpus):
                where = "label only, by choice (corpora.json: body=false)"
            elif root:
                where = f"body indexed from {root}"
            else:
                where = "label only — NO vault root found"
            print(f"[semantic] {args.corpus}: {notes} notes, {chunks} vectors "
                  f"({body} body sections) · {where} · backend {model} "
                  f"({BACKENDS.get(model, {}).get('kind', '?')}) · {index_path(args.corpus, model).name}")
            if UNREADABLE:
                # Loud, not fatal. These notes are still findable by their label; they are simply
                # not findable by anything written inside them, and that has to be visible.
                print(f"[semantic] WARNING: {len(UNREADABLE)} note(s) indexed WITHOUT their body "
                      f"— file not readable under {root}. First few: "
                      f"{', '.join(UNREADABLE[:3])}")
            return 0

        # Scope gate. `index` is deliberately NOT gated: rebuilding vectors is maintenance and a
        # scheduled pass must be able to run from anywhere. Answering is what gets bound.
        refusal = corpus_out_of_scope(args.corpus)
        if refusal:
            # A refusal is not an empty vault, and must never arrive looking like one. Same
            # discipline as the rejected/no-index/unavailable split already enforced below.
            if getattr(args, "json", False):
                print(json.dumps({"query": getattr(args, "query", ""), "corpus": args.corpus,
                                  "model": model, "judge": None, "verdict": "out-of-scope",
                                  "note": refusal, "results": []}, ensure_ascii=False))
            else:
                print(f"[semantic] {refusal}")
            return 0

        if args.cmd == "suggest":
            return suggest(args.corpus, args.note, args.k)

        wanted = args.k
        if getattr(args, "rerank", False) and RERANK_MODEL:
            # Read a wider shortlist so the judge has something to choose between.
            results = search(args.query, args.corpus, max(RERANK_CANDIDATES, wanted))
            results = rerank(args.query, results)[:wanted]
            if RERANK_ABANDONED or RERANK_UNREACHABLE:
                print(f"[semantic] WARNING: rerank scored nothing for {len(RERANK_ABANDONED)} "
                      f"candidate(s) that ran out of time and {len(RERANK_UNREACHABLE)} the judge "
                      f"could not be reached for — the ranking below is cosine order for those. "
                      f"First few: {', '.join((RERANK_ABANDONED + RERANK_UNREACHABLE)[:3])}",
                      file=sys.stderr)
        else:
            # THE CASE THIS FILE ALREADY WARNED ABOUT ONE STEP LATER AND NOT HERE. The block above
            # is loud when the judge times out or cannot be reached; it said nothing when there is
            # NO judge to reach, because `RERANK_MODEL` defaults to "" and the `and` above simply
            # falls through to plain cosine. Measured 2026-08-25: `CLAUDE_RERANK_MODEL` is set
            # nowhere in settings.json, so `--rerank` had never once run from a Claude Code
            # session -- the flag was accepted, the output was byte-identical to a plain search,
            # and nothing said which of the two it was.
            #
            # Every caller that asks for the second stage asks because cosine CANNOT ABSTAIN: it
            # always ranks something first, and on this vault a real hit (0.655) and pure noise
            # (0.551) sit three hundredths apart. Handing back stage one in silence is the exact
            # failure the second stage exists to remove.
            if getattr(args, "rerank", False):
                print("[semantic] WARNING: --rerank was asked for and NO judge is configured "
                      "(CLAUDE_RERANK_MODEL is empty) -- the ranking below is plain cosine, which "
                      "cannot abstain. Set CLAUDE_RERANK_MODEL (the recall judge in judge.go uses "
                      "gemma-4-12b-it) or read the scores as an ORDER, never as a verdict.",
                      file=sys.stderr)
            results = search(args.query, args.corpus, wanted)

        wants_json = getattr(args, "json", False)
        wanted_judge = bool(getattr(args, "rerank", False))
        asked = RERANK_MODEL if wanted_judge and RERANK_MODEL else None
        answered = RERANK_ANSWERED[0] if RERANK_ANSWERED else None
        if not results:
            # THE VERDICT MUST SURVIVE THE FORMAT. "The judge read every candidate and rejected all
            # of them" and "there is no index to search" are opposite facts, and an empty list says
            # neither. The prose branch already distinguished them; the JSON branch has to as well,
            # or a caller reading the machine format loses the one thing this stage adds.
            if answered:
                verdict, note = "rejected", (
                    f"nothing in the vault answers this — every candidate scored below "
                    f"{RERANK_FLOOR:.0f}/10 with {answered}")
            else:
                verdict, note = "no-index", (
                    f"no notes in the {args.corpus} corpus index — is the wiki compiled?")
            if wants_json:
                print(json.dumps({"query": args.query, "corpus": args.corpus, "model": model,
                                  "judge": answered, "judge_asked": asked,
                                  "verdict": verdict, "note": note, "results": []},
                                 ensure_ascii=False))
            else:
                print(f"[semantic] {model}{' + ' + answered if answered else ''} · {note}")
            return 0
        # Logged once, before the format branches, so the CLI and the MCP leave identical
        # evidence — a fire recorded in only one of the two callers would make the ledger a
        # measure of which interface was used rather than of which note was useful.
        log_fires(results, args.corpus)

        if wants_json:
            print(json.dumps({"query": args.query, "corpus": args.corpus, "model": model,
                              "judge": answered, "judge_asked": asked,
                              "abandoned": len(RERANK_ABANDONED),
                              "unreachable": len(RERANK_UNREACHABLE),
                              "verdict": "hits", "note": rerank_note(answered) if wanted_judge else None,
                              "results": results},
                             ensure_ascii=False))
            return 0
        judged = " + " + answered if any("judge" in r for r in results) else ""
        print(f"[semantic] {model}{judged} · query: \"{args.query}\" · top {len(results)}")
        for r in results:
            mark = f"  judge {r['judge']:.0f}/10" if "judge" in r else ""
            print(f"  {r['score']:.3f}{mark}  {r['path']}  —  {r['name']}: {first_sentence(r['summary'])}")
            if r.get("matched"):
                print(f"        matched in: {', '.join(r['matched'][:3])}")
        return 0
    except OllamaUnavailable as exc:
        if getattr(args, "json", False):
            # Unreachable is NOT the same as empty, and a caller that cannot tell them apart will
            # report "the vault has nothing on this" when the embedder is simply not running.
            print(json.dumps({"query": getattr(args, "query", ""), "corpus": args.corpus,
                              "model": model, "judge": None, "verdict": "unavailable",
                              "note": f"no embedding backend usable here ({exc}) - fall back to lexical recall",
                              "results": []}, ensure_ascii=False))
            return 0
        print(f"[semantic] no embedding backend ({exc}). Either start ollama with `bge-m3`, or set "
              f"VOYAGE_API_KEY for the cloud fallback. Falling back to lexical recall.",
              file=sys.stderr)
        print("[semantic] unavailable — use lexical recall for this query.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
