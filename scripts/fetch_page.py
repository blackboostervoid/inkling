# @module: fetch_page
# @role: Tier-2 fetch rail for the researcher agents — retrieves a page the built-in WebFetch cannot
#        (UA-blocked, or JS-rendered prose) and returns its MACHINE-READABLE payload: JSON-LD, meta
#        tags, embedded app state, then visible text as the last resort.
# @deps: (stdlib only — zero third-party, per the hook/script portability rule)
# @used-by: agents/researcher.md (fetch ladder, tier 2) - called via Bash
# @doc: $CLAUDE_CONFIG_DIR/agent-memory/researcher/MEMORY.md §2 (channel priority)
"""
fetch_page.py — the fetcher that runs when WebFetch comes back empty or blocked.

Two failures were measured on 2026-07-27, and they are the same failure wearing opposite masks:

  * github / pypi  — the fetch SUCCEEDS and the asked-for fact is silently absent (JS-rendered),
                     so a confident answer gets invented in its place.
  * rozetka.com.ua — the fetch FAILS loudly with 403, because the default fetcher does not send a
                     browser User-Agent. The very same request with one returns 200 and 44 KB.

Both are cured by the same move: stop asking the page for prose and ask it for the machine-readable
copy of itself. Modern pages ship one for search engines — schema.org JSON-LD, OpenGraph meta, an
embedded `__NEXT_DATA__` / `__NUXT__` / `__INITIAL_STATE__` blob. That copy sits in the RAW HTML, so
no JavaScript engine is involved, and it is strictly better for an agent than rendered prose:
typed fields instead of sentences, a fraction of the tokens, nothing to paraphrase away.

Escalation order (stop at the first that yields the fact):
    1. JSON-LD  — typed schema.org objects. Product/Offer/Article/Recipe carry exact values.
    2. Meta     — OpenGraph + twitter + <title>. Always present, always small.
    3. State    — embedded app state. Big, so reported by size and only dumped on --state.
    4. Text     — visible text with script/style stripped. The fallback, never the default.

`--expect` makes the presence gate MECHANICAL rather than prose: the value must literally occur in
what came back, or the script exits 2 and says so. A fetch that cannot show the value contributed
zero sources, however articulate its prose looked.

Usage:
    python3 fetch_page.py <url>
    python3 fetch_page.py <url> --expect "KINGSEVEN"      # presence gate; exit 2 when absent
    python3 fetch_page.py <url> --text                    # include full visible text
    python3 fetch_page.py <url> --state                   # include embedded app state
    python3 fetch_page.py <url> --save out.html           # keep the raw HTML for a second pass
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# A real desktop Chrome header set. The single most common reason a public page refuses a script is
# a missing or non-browser User-Agent -- rozetka answers 403 to curl's default and 200 to this.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,uk;q=0.8,ru;q=0.7",
    # Deliberately no `br`: brotli is not in the stdlib, and advertising it would hand us bytes we
    # cannot decode. gzip and deflate are both handled below.
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}

# Tag-stripped text is small even for heavy pages (a 319 KB rozetka product page yields ~4.4 K),
# so a preview this size returns most pages WHOLE. A preview that silently cut a page in a quarter
# is what made a probe agent report a spec table as "missing" when it was extracted fine — hence
# also `text_truncated` in the payload, so a cut is stated rather than inferred.
TEXT_PREVIEW_CHARS = 6000
MAX_TEXT_CHARS = 40000
TIMEOUT_SECONDS = 30

SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
STYLE_RE = re.compile(r"<style\b.*?</style>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
LD_JSON_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
META_RE = re.compile(r"<meta\b([^>]+)>", re.I)
ATTR_RE = re.compile(r'(\w[\w:-]*)\s*=\s*["\']([^"\']*)["\']')
CHARSET_RE = re.compile(rb'charset=["\']?([\w-]+)', re.I)

# Embedded application state, in the order a page is likely to carry it.
STATE_PATTERNS = [
    ("__NEXT_DATA__", re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S | re.I)),
    ("__NUXT__", re.compile(r"window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>", re.S)),
    ("__INITIAL_STATE__", re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.S)),
    ("__APOLLO_STATE__", re.compile(r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.S)),
]


# ============================================================
# CONFIG
# ============================================================
@dataclass(frozen=True)
class FetchRequest:
    url: str
    expect: str = ""
    include_text: bool = False
    include_state: bool = False
    save_to: str = ""


@dataclass
class PageResult:
    url: str
    status: int = 0
    error: str = ""
    title: str = ""
    json_ld: List[dict] = field(default_factory=list)
    meta: Dict[str, str] = field(default_factory=dict)
    state: Dict[str, object] = field(default_factory=dict)
    json_body: object = None
    # Full text, never truncated. Truncation happens at OUTPUT time only: the presence gate must
    # weigh everything that came back, or a value sitting past the preview cutoff reads as absent.
    text: str = ""
    text_chars: int = 0
    html_chars: int = 0


# ============================================================
# NETWORK
# ============================================================
class PageFetcher:
    """Retrieves raw HTML with a browser identity, decoding whatever encoding comes back."""

    def fetch(self, url: str) -> tuple:
        """Return (html, status, error). html is '' when the fetch failed."""
        request = urllib.request.Request(url, headers=BROWSER_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = self._decompress(response.read(), response.headers.get("Content-Encoding", ""))
                return self._decode(raw, response.headers.get("Content-Type", "")), response.status, ""
        except urllib.error.HTTPError as exc:
            return "", exc.code, f"HTTP {exc.code} {exc.reason}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return "", 0, f"network error: {exc}"

    def _decompress(self, payload: bytes, encoding: str) -> bytes:
        encoding = encoding.lower()
        if "gzip" in encoding:
            return gzip.decompress(payload)
        if "deflate" in encoding:
            return zlib.decompress(payload, -zlib.MAX_WBITS)
        return payload

    def _decode(self, payload: bytes, content_type: str) -> str:
        """Charset from the header, else from the document's own meta, else UTF-8."""
        match = CHARSET_RE.search(content_type.encode("ascii", "ignore")) or CHARSET_RE.search(payload[:4096])
        charset = match.group(1).decode("ascii", "ignore") if match else "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


# ============================================================
# EXTRACTION — machine-readable layers, in escalation order
# ============================================================
class StructuredExtractor:
    """Pulls the copy of itself that a page publishes for machines."""

    def json_ld(self, html: str) -> List[dict]:
        """schema.org blocks. A page may carry several; a block may itself be a list or a @graph."""
        blocks: List[dict] = []
        for raw in LD_JSON_RE.findall(html):
            try:
                parsed = json.loads(raw.strip())
            except ValueError:
                continue
            blocks.extend(self._flatten(parsed))
        return blocks

    def _flatten(self, node) -> List[dict]:
        if isinstance(node, list):
            return [item for entry in node for item in self._flatten(entry)]
        if isinstance(node, dict):
            if "@graph" in node:
                return self._flatten(node["@graph"])
            return [node]
        return []

    def meta(self, html: str) -> Dict[str, str]:
        """OpenGraph, twitter and the standard description — small, typed, nearly always present."""
        found: Dict[str, str] = {}
        for attrs in META_RE.findall(html):
            pairs = dict(ATTR_RE.findall(attrs))
            key = pairs.get("property") or pairs.get("name") or ""
            value = pairs.get("content", "")
            if not value:
                continue
            if key.startswith(("og:", "twitter:", "product:")) or key in ("description", "keywords"):
                found[key] = unescape(value)
        return found

    def state(self, html: str) -> Dict[str, object]:
        """Embedded app state. Recorded by name and size; the payload itself is opt-in (--state)."""
        found: Dict[str, object] = {}
        for name, pattern in STATE_PATTERNS:
            match = pattern.search(html)
            if not match:
                continue
            raw = match.group(1).strip()
            try:
                found[name] = {"chars": len(raw), "data": json.loads(raw)}
            except ValueError:
                found[name] = {"chars": len(raw), "data": None, "note": "present but not valid JSON"}
        return found

    def title(self, html: str) -> str:
        match = TITLE_RE.search(html)
        return unescape(re.sub(r"\s+", " ", match.group(1)).strip()) if match else ""

    def text(self, html: str) -> str:
        stripped = STYLE_RE.sub(" ", SCRIPT_RE.sub(" ", html))
        return re.sub(r"\s+", " ", unescape(TAG_RE.sub(" ", stripped))).strip()


def unescape(value: str) -> str:
    for entity, char in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&nbsp;", " ")):
        value = value.replace(entity, char)
    return value


# ============================================================
# ORCHESTRATOR
# ============================================================
class PageReader:
    """Runs the escalation ladder and assembles one result. Does no work itself."""

    def __init__(self) -> None:
        self.fetcher = PageFetcher()
        self.extractor = StructuredExtractor()

    def read(self, request: FetchRequest) -> PageResult:
        body, status, error = self.fetcher.fetch(request.url)
        result = PageResult(url=request.url, status=status, error=error)
        if not body:
            return result

        if request.save_to:
            self._save(body, request.save_to)

        result.html_chars = len(body)

        # A JSON response is already the machine-readable layer — rung 1 of the ladder comes back
        # through this same command, so parse it instead of running HTML extractors over it.
        parsed = self._as_json(body)
        if parsed is not None:
            result.json_body = parsed
            result.text = body
            result.text_chars = len(body)
            return result

        result.title = self.extractor.title(body)
        result.json_ld = self.extractor.json_ld(body)
        result.meta = self.extractor.meta(body)
        result.state = self.extractor.state(body)
        result.text = self.extractor.text(body)
        result.text_chars = len(result.text)
        return result

    def _as_json(self, body: str) -> object:
        stripped = body.lstrip()
        if not stripped.startswith(("{", "[")):
            return None
        try:
            return json.loads(stripped)
        except ValueError:
            return None

    def _save(self, html: str, path: str) -> None:
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(html)
        except OSError as exc:
            print(f"[WARNING] could not save raw HTML: {exc}", file=sys.stderr)


# ============================================================
# PRESENCE GATE
# ============================================================
def contains_expected(result: PageResult, expected: str) -> bool:
    """True when the value literally occurs somewhere in what came back.

    This is the mechanical form of the researcher's presence gate: a value that cannot be shown in
    the payload did not come from this URL, no matter how plausible it reads in a summary.
    """
    haystack = json.dumps({
        "title": result.title,
        "json_ld": result.json_ld,
        "meta": result.meta,
        "state": result.state,
        "json_body": result.json_body,
    }, ensure_ascii=False, default=str) + result.text
    return expected.lower() in haystack.lower()


# ============================================================
# ENTRY POINT
# ============================================================
def build_payload(result: PageResult, request: FetchRequest) -> dict:
    """Assemble the printed payload. Truncation lives HERE and nowhere else — the presence gate
    weighs the full text, so a value past the preview cutoff is never mistaken for a missing one."""
    limit = MAX_TEXT_CHARS if request.include_text else TEXT_PREVIEW_CHARS
    payload = {
        "url": result.url,
        "status": result.status,
        "title": result.title,
        "json_ld": result.json_ld,
        "meta": result.meta,
        "html_chars": result.html_chars,
        "text_chars": result.text_chars,
        "text": result.text[:limit],
        "text_truncated": result.text_chars > limit,
    }
    if result.json_body is not None:
        payload["json_body"] = result.json_body
    if result.error:
        payload["error"] = result.error
    if request.include_state:
        payload["state"] = result.state
    else:
        payload["state_present"] = {name: blob["chars"] for name, blob in result.state.items()}
    return payload


def parse_args(argv: List[str]) -> FetchRequest:
    parser = argparse.ArgumentParser(description="Fetch a page and return its machine-readable payload.")
    parser.add_argument("url")
    parser.add_argument("--expect", default="", help="value that MUST appear in the payload; exit 2 if absent")
    parser.add_argument("--text", action="store_true", help="include full visible text, not a preview")
    parser.add_argument("--state", action="store_true", help="include embedded app state payloads")
    parser.add_argument("--save", default="", help="write the raw HTML to this path")
    args = parser.parse_args(argv)
    return FetchRequest(url=args.url, expect=args.expect, include_text=args.text,
                        include_state=args.state, save_to=args.save)


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    request = parse_args(argv if argv is not None else sys.argv[1:])
    result = PageReader().read(request)

    if result.error:
        print(f"[ERROR] {request.url} -> {result.error}", file=sys.stderr)
        print(json.dumps({"url": result.url, "status": result.status, "error": result.error,
                          "found": False}, ensure_ascii=False, indent=2))
        return 1

    if request.expect and not contains_expected(result, request.expect):
        print(f"[ERROR] presence gate failed: '{request.expect}' is absent from the payload. "
              f"Treat this URL as ZERO sources.", file=sys.stderr)
        print(json.dumps({"url": result.url, "status": result.status, "expected": request.expect,
                          "found": False, "title": result.title}, ensure_ascii=False, indent=2))
        return 2

    print(json.dumps(build_payload(result, request), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
