# @module: parse_book
# @role: book file (pdf/epub/fb2/md/txt) or YouTube URL -> reading-unit slices + meta.json
# @deps: (stdlib + pymupdf4llm / fitz / ebooklib + bs4 + youtube-transcript-api / yt-dlp per-source, lazy-imported)
# @used-by: book-harvest SKILL.md (Step 1)
# @doc: SKILL.md#step-1-slice-the-book-into-reading-units
"""
Slice a long-form book into reading-unit markdown files plus a meta.json manifest.

Pipeline (format-agnostic after extraction):
  extract raw blocks  ->  drop boilerplate (TOC/copyright/front-matter, any size)
  ->  pack survivors into ~READING_UNIT_WORDS reading units  ->  drop sub-min units
  ->  write ch_NN.md + meta.json

The packing step is load-bearing: PDF backends (pymupdf4llm) emit a heading per *bold
sub-heading*, which fragments a book into hundreds of micro-blocks. Packing coalesces those
back into coherent reading units so the scout fan-out stays ~20 agents, not ~140.

Branching by extension (each backend lazy-imported so a missing optional dep only fails
the formats that need it):
  .pdf            -> pymupdf4llm (markdown) + fitz (metadata)
  .epub           -> ebooklib + bs4
  .fb2 / .fb2.zip -> xml.etree + zipfile
  .md / .txt      -> native read
  YouTube URL/id  -> youtube-transcript-api (transcript) + yt-dlp (title/channel/chapters)

A YouTube video is just another source: the timestamped transcript is woven with the
video's yt-dlp chapter titles as "## headings", then handed to the SAME split -> pack ->
slice pipeline as a book. No chapters -> the flat transcript falls back to size chunking.

Output: <out>/ch_NN.md per reading unit + <out>/meta.json.
The main orchestration context reads ONLY meta.json; unit bodies are for haiku scouts.

Invoke with the `py` launcher (Python 3.14):
    py parse_book.py <book-path> --out <dir>
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Tuple
from xml.etree import ElementTree

MIN_UNIT_WORDS = 200          # a final reading unit must clear this to be kept
READING_UNIT_WORDS = 2800     # target size when packing fragments into a unit
FALLBACK_CHUNK_WORDS = 2800   # flat-text fallback chunk size (no headings present)
HEADING_PATTERN = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)

# Structural front-matter / back-matter words matched against the (short) block TITLE.
# Bilingual: the same parser serves Russian and English books.
TITLE_JUNK_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"оглавл", r"содержание", r"предислов", r"вступлен", r"^\W*от автора",
        r"об авторе", r"от переводчика", r"благодарност", r"выходные данные",
        r"библиограф", r"список литератур", r"предметный указател", r"примечани",
        r"table of contents", r"about the author", r"acknowledg", r"bibliography",
        r"^\W*index\W*$", r"^\W*foreword", r"^\W*preface",
        # Back-matter: endnote blocks and single-letter index headings.
        # PDF backends emit "NOTES", repeated "GO TO NOTE REFERENCE IN TEXT" anchors,
        # and per-letter index sections ("B", "C", ... "S / T / U") — none carry ideas.
        r"^\W*notes?\W*$", r"note reference in text", r"go to note reference",
        r"^\W*[a-z]\W*$",
    )
]
# Legal / boilerplate matched against title + body head (content-bearing, not title-only).
BODY_JUNK_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\bcopyright\b", r"\ball rights reserved\b", r"\bisbn\b",
        r"©\s*\d{4}", r"все права защищены",
    )
]


# ============================================================
# YOUTUBE SOURCE (vendored fetch core: URL/id -> transcript markdown)
# A YouTube video is treated as just another input format. We fetch the
# timestamped transcript, mark yt-dlp chapter boundaries as "## headings", and
# feed the result into the SAME split -> pack -> slice pipeline as a book.
# ============================================================
YT_LANG_PREF = ["en", "ru"]
_YT_URL_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)"
    r"([A-Za-z0-9_-]{11})"
)
_YT_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def is_youtube_input(source: str) -> bool:
    """True if the CLI argument is a YouTube URL or a bare 11-char video id."""
    s = source.strip()
    if "youtube.com" in s or "youtu.be" in s:
        return True
    # A bare id has no dot, so real filenames (report.txt) never match here.
    return bool(_YT_BARE_ID_RE.match(s)) and not Path(s).exists()


def extract_video_id(source: str) -> str:
    """Pull the 11-char video id from any YouTube URL form, or return a bare id."""
    match = _YT_URL_RE.search(source)
    if match:
        return match.group(1)
    s = source.strip()
    return s if _YT_BARE_ID_RE.match(s) else ""


def fetch_youtube_metadata(video_id: str) -> dict:
    """Video title / channel / chapters via yt-dlp; oEmbed fallback for title only."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    if shutil.which("yt-dlp"):
        try:
            result = subprocess.run(
                ["yt-dlp", "--skip-download", "--dump-json", "--no-warnings", url],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                return {
                    "title": data.get("title", "Untitled"),
                    "channel": data.get("channel", data.get("uploader", "Unknown")),
                    "chapters": data.get("chapters") or [],
                }
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            pass
    return _youtube_metadata_oembed(video_id)


def _youtube_metadata_oembed(video_id: str) -> dict:
    """Best-effort title/channel via the public oEmbed endpoint (no chapters)."""
    import requests
    url = (
        "https://www.youtube.com/oembed"
        f"?url=https://www.youtube.com/watch?v={video_id}&format=json"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return {
            "title": data.get("title", "Untitled"),
            "channel": data.get("author_name", "Unknown"),
            "chapters": [],
        }
    except (requests.RequestException, ValueError):
        return {"title": video_id, "channel": "Unknown", "chapters": []}


def fetch_youtube_transcript(video_id: str, lang_pref: List[str]) -> List[Tuple[float, str]]:
    """Return [(start_seconds, text)] for the best available transcript.

    Prefers languages in lang_pref; falls back to the first transcript track the
    video exposes (manual or auto-generated). Raises RuntimeError with context.
    """
    from youtube_transcript_api import YouTubeTranscriptApi
    try:
        from youtube_transcript_api import (
            NoTranscriptFound, TranscriptsDisabled, CouldNotRetrieveTranscript,
        )
    except ImportError:
        from youtube_transcript_api._errors import (
            NoTranscriptFound, TranscriptsDisabled, CouldNotRetrieveTranscript,
        )
    try:
        transcript_list = YouTubeTranscriptApi().list(video_id)
    except (TranscriptsDisabled, CouldNotRetrieveTranscript) as exc:
        raise RuntimeError(f"No transcript available for video {video_id}: {exc}") from exc
    try:
        transcript = transcript_list.find_transcript(lang_pref)
    except NoTranscriptFound:
        transcript = next(iter(transcript_list), None)
    if transcript is None:
        raise RuntimeError(f"No transcript tracks found for video {video_id}")
    fetched = transcript.fetch()
    return [(snippet.start, snippet.text) for snippet in fetched]


def build_transcript_markdown(snippets: List[Tuple[float, str]], chapters: list) -> str:
    """Weave yt-dlp chapter titles into the transcript as '## headings'.

    With chapters, the downstream heading-splitter gets natural section breaks;
    without them, the flat transcript falls through to size-based chunking.
    """
    if not chapters:
        return "\n".join(text for _, text in snippets)
    ordered = sorted(chapters, key=lambda c: c.get("start_time", 0))
    parts: List[str] = []
    next_ch = 0
    for start, text in snippets:
        while next_ch < len(ordered) and start >= ordered[next_ch].get("start_time", 0):
            parts.append(f"\n## {ordered[next_ch].get('title', 'Chapter')}\n")
            next_ch += 1
        parts.append(text)
    return "\n".join(parts)


# ============================================================
# DATA CONTRACTS
# ============================================================
@dataclass(frozen=True)
class Chapter:
    n: int
    file: str
    title: str
    word_count: int


@dataclass
class BookMeta:
    title: str
    author: str
    source_format: str
    chapters: List[Chapter] = field(default_factory=list)


# ============================================================
# JUNK FILTER (boilerplate detection, shared across all formats)
# ============================================================
class JunkFilter:
    # @role: decide whether a raw block is structural boilerplate (drop pre-packing)

    def is_boilerplate(self, title: str, body: str) -> bool:
        clean_title = title.strip().strip("*").strip()
        if any(pat.search(clean_title) for pat in TITLE_JUNK_PATTERNS):
            return True
        head = clean_title + "\n" + body[:400]
        return any(pat.search(head) for pat in BODY_JUNK_PATTERNS)

    @staticmethod
    def word_count(text: str) -> int:
        return len(text.split())


# ============================================================
# READING-UNIT PACKER (coalesce fragments -> ~target-word units)
# ============================================================
class ReadingUnitPacker:
    # @role: merge fine-grained content blocks into coherent reading units
    # @data-in: list[(title, body)] already stripped of boilerplate
    # @data-out: list[(unit_title, unit_body)] each ~READING_UNIT_WORDS

    def pack(self, blocks: List[Tuple[str, str]], target_words: int) -> List[Tuple[str, str]]:
        units = []
        buf_titles: List[str] = []
        buf_parts: List[str] = []
        buf_words = 0
        for title, body in blocks:
            clean_title = title.strip().strip("*").strip() or "Раздел"
            buf_titles.append(clean_title)
            buf_parts.append(f"## {clean_title}\n\n{body}".strip())
            buf_words += len(body.split())
            if buf_words >= target_words:
                units.append(self._flush(buf_titles, buf_parts))
                buf_titles, buf_parts, buf_words = [], [], 0
        if buf_parts:
            units.append(self._flush(buf_titles, buf_parts))
        return units

    @staticmethod
    def _flush(titles: List[str], parts: List[str]) -> Tuple[str, str]:
        shown = " / ".join(titles[:3])
        if len(titles) > 3:
            shown += " / ..."
        return shown, "\n\n".join(parts)


# ============================================================
# FORMAT EXTRACTORS (one method per backend, dispatched by extension)
# ============================================================
class BookExtractor:
    # @role: raw book file or YouTube source -> list of (title, body) blocks, per-source
    # @data-in: Path to book file, or a YouTube URL/video id string
    # @data-out: list[(title, body)] plus (title, author, source_format)

    def extract(self, source):
        if is_youtube_input(str(source)):
            return self._from_youtube(str(source))
        path = Path(source)
        suffix = path.suffix.lower()
        name = path.name.lower()
        if suffix == ".pdf":
            return self._from_pdf(path)
        if suffix == ".epub":
            return self._from_epub(path)
        if name.endswith(".fb2.zip") or suffix == ".fb2":
            return self._from_fb2(path)
        if suffix in (".md", ".txt"):
            return self._from_text(path)
        raise ValueError(f"Unsupported format: {suffix or name}")

    def _from_youtube(self, source: str):
        video_id = extract_video_id(source)
        if not video_id:
            raise ValueError(f"Could not extract a YouTube video id from: {source}")
        meta = fetch_youtube_metadata(video_id)
        snippets = fetch_youtube_transcript(video_id, YT_LANG_PREF)
        if not snippets:
            raise RuntimeError(f"Empty transcript for video {video_id}")
        markdown = build_transcript_markdown(snippets, meta["chapters"])
        return self._split_markdown(markdown), (meta["title"], meta["channel"], "youtube")

    def _from_pdf(self, path: Path):
        import pymupdf4llm
        md = pymupdf4llm.to_markdown(str(path))
        title, author = self._pdf_meta(path, path.stem)
        return self._split_markdown(md), (title, author, "pdf")

    def _from_epub(self, path: Path):
        import ebooklib
        from ebooklib import epub
        from bs4 import BeautifulSoup
        book = epub.read_epub(str(path))
        title = self._epub_meta(book, "title") or path.stem
        author = self._epub_meta(book, "creator") or "unknown"
        blocks = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            soup = BeautifulSoup(item.get_content(), "html.parser")
            heading = soup.find(["h1", "h2", "h3"])
            head_text = heading.get_text(strip=True) if heading else item.get_name()
            body = soup.get_text("\n", strip=True)
            blocks.append((head_text, body))
        return blocks, (title, author, "epub")

    def _from_fb2(self, path: Path):
        xml_bytes = self._read_fb2_bytes(path)
        root = ElementTree.fromstring(xml_bytes)
        ns = self._fb2_namespace(root)
        title = self._fb2_text(root, f"{ns}description/{ns}title-info/{ns}book-title") or path.stem
        author = self._fb2_author(root, ns) or "unknown"
        blocks = []
        for section in root.iter(f"{ns}section"):
            paras = [self._collapse(p) for p in section.iter(f"{ns}p")]
            body = "\n".join(t for t in paras if t)
            head = section.find(f"{ns}title")
            head_text = self._collapse(head) if head is not None else "Untitled section"
            blocks.append((head_text, body))
        return blocks, (title, author, "fb2")

    def _from_text(self, path: Path):
        raw = path.read_text(encoding="utf-8", errors="replace")
        fmt = "md" if path.suffix.lower() == ".md" else "txt"
        return self._split_markdown(raw), (path.stem, "unknown", fmt)

    # ---- helpers ----
    def _split_markdown(self, text: str):
        if HEADING_PATTERN.search(text):
            return self._split_by_headings(text)
        return self._split_by_size(text)

    @staticmethod
    def _split_by_headings(text: str):
        matches = list(HEADING_PATTERN.finditer(text))
        blocks = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            chunk = text[start:end]
            first_nl = chunk.find("\n")
            title = chunk[:first_nl].lstrip("# ").strip() if first_nl != -1 else chunk.lstrip("# ").strip()
            body = chunk[first_nl + 1:] if first_nl != -1 else ""
            blocks.append((title, body.strip()))
        return blocks

    @staticmethod
    def _split_by_size(text: str):
        words = text.split()
        blocks = []
        for i in range(0, len(words), FALLBACK_CHUNK_WORDS):
            chunk = " ".join(words[i:i + FALLBACK_CHUNK_WORDS])
            blocks.append((f"Part {i // FALLBACK_CHUNK_WORDS + 1}", chunk))
        return blocks

    @staticmethod
    def _pdf_meta(path: Path, default_title: str) -> Tuple[str, str]:
        try:
            import fitz
            with fitz.open(str(path)) as doc:
                meta = doc.metadata or {}
        except (ImportError, RuntimeError, OSError):
            return default_title, "unknown"
        title = (meta.get("title") or "").strip() or default_title
        author = (meta.get("author") or "").strip() or "unknown"
        return title, author

    @staticmethod
    def _epub_meta(book, name: str) -> str:
        data = book.get_metadata("DC", name)
        return data[0][0] if data else ""

    @staticmethod
    def _read_fb2_bytes(path: Path) -> bytes:
        if path.name.lower().endswith(".fb2.zip") or zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                inner = next((n for n in zf.namelist() if n.lower().endswith(".fb2")), None)
                if inner is None:
                    raise ValueError("No .fb2 entry inside zip")
                return zf.read(inner)
        return path.read_bytes()

    @staticmethod
    def _fb2_namespace(root) -> str:
        if root.tag.startswith("{"):
            return root.tag[:root.tag.index("}") + 1]
        return ""

    @staticmethod
    def _fb2_text(root, path_expr: str) -> str:
        el = root.find(path_expr)
        return el.text.strip() if el is not None and el.text else ""

    def _fb2_author(self, root, ns: str) -> str:
        author = root.find(f"{ns}description/{ns}title-info/{ns}author")
        if author is None:
            return ""
        parts = [self._fb2_text(author, f"{ns}first-name"), self._fb2_text(author, f"{ns}last-name")]
        return " ".join(p for p in parts if p)

    @staticmethod
    def _collapse(el) -> str:
        return " ".join("".join(el.itertext()).split())


# ============================================================
# ORCHESTRATOR — extract -> drop boilerplate -> pack -> write slices + meta.json
# ============================================================
class BookSlicer:
    # @role: drive extraction, boilerplate drop, packing, and write units + meta
    # @calls: BookExtractor.extract, JunkFilter.is_boilerplate, ReadingUnitPacker.pack
    # @data-in: book path or YouTube source, output dir
    # @data-out: meta.json + ch_NN.md files on disk; returns BookMeta

    def __init__(self, extractor: BookExtractor, junk_filter: JunkFilter, packer: ReadingUnitPacker):
        self.extractor = extractor
        self.junk_filter = junk_filter
        self.packer = packer

    def slice(self, source, out_dir: Path) -> BookMeta:
        blocks, (title, author, fmt) = self.extractor.extract(source)
        content = [(t, b) for (t, b) in blocks if not self.junk_filter.is_boilerplate(t, b)]
        units = self.packer.pack(content, READING_UNIT_WORDS)
        out_dir.mkdir(parents=True, exist_ok=True)
        meta = BookMeta(title=title, author=author, source_format=fmt)
        n = 0
        for unit_title, unit_body in units:
            words = self.junk_filter.word_count(unit_body)
            if words < MIN_UNIT_WORDS:
                continue
            n += 1
            file_name = f"ch_{n:02d}.md"
            (out_dir / file_name).write_text(
                f"# {unit_title}\n\n{unit_body}\n", encoding="utf-8"
            )
            meta.chapters.append(
                Chapter(n=n, file=file_name, title=unit_title, word_count=words)
            )
        (out_dir / "meta.json").write_text(
            json.dumps(asdict(meta), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return meta


# ============================================================
# ENTRY POINT (error handling stays at the edge)
# ============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Slice a book or YouTube transcript into reading-unit files + meta.json")
    parser.add_argument("book", help="Path to the book file (pdf/epub/fb2/md/txt) or a YouTube URL/video id")
    parser.add_argument("--out", required=True, help="Output directory for reading-unit slices + meta.json")
    args = parser.parse_args()

    source = args.book
    if not is_youtube_input(source):
        book_path = Path(source)
        if not book_path.is_file():
            print(f"[ERROR] File not found: {book_path}")
            return 1
        source = book_path

    slicer = BookSlicer(BookExtractor(), JunkFilter(), ReadingUnitPacker())
    try:
        meta = slicer.slice(source, Path(args.out))
    except (ValueError, OSError, ElementTree.ParseError, RuntimeError) as exc:
        print(f"[ERROR] Failed to parse source: {exc}")
        return 1
    except ImportError as exc:
        print(f"[ERROR] Missing dependency for this source: {exc}. "
              f"Books: pymupdf4llm / ebooklib + beautifulsoup4. "
              f"YouTube: youtube-transcript-api (py -m pip install youtube-transcript-api).")
        return 1

    print(f"[SUCCESS] {meta.title} ({meta.source_format}) -> {len(meta.chapters)} reading units")
    print(f"[INFO] Meta written to {Path(args.out) / 'meta.json'}")
    if not meta.chapters:
        print("[WARNING] No usable reading units after boilerplate filter")
    return 0


if __name__ == "__main__":
    sys.exit(main())
