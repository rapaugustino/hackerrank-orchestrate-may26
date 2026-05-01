"""BM25 retrieval over the three support corpora.

One index per domain. Lazy build, in-memory cache. Chunks are markdown sections
(split on `## `), with the file title and section heading prepended to the chunk
body so the retriever can match on title-level signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from rank_bm25 import BM25Okapi

DOMAINS = ("hackerrank", "claude", "visa")
DOMAIN_TO_DISPLAY = {"hackerrank": "HackerRank", "claude": "Claude", "visa": "Visa"}
DISPLAY_TO_DOMAIN = {v: k for k, v in DOMAIN_TO_DISPLAY.items()}

# Aliases mapping the raw normalized folder name to the short label form
# used in the labeled sample. Folder names are lowercased and kebab->snake.
_FOLDER_ALIASES = {
    "hackerrank_community": "community",
    "privacy_and_legal": "privacy",
    "pro_and_max_plans": "plans",
    "team_and_enterprise_plans": "enterprise_plans",
    "claude_api_and_console": "api_and_console",
    "identity_management_sso_jit_scim": "identity_management",
}

# For Visa the on-disk folder structure (only `support/`) is uninformative,
# so we expose a small fixed label list and let the generation stage pick.
_VISA_LABELS = ["general_support", "travel_support", "card_management", "merchant_support"]


@dataclass
class Chunk:
    domain: str
    path: str
    title: str
    section: str
    text: str

    def for_prompt(self) -> str:
        header = f"[{DOMAIN_TO_DISPLAY[self.domain]}] {self.title}"
        if self.section and self.section != self.title:
            header += f" -> {self.section}"
        return f"{header}\nsource: {self.path}\n\n{self.text}"


@dataclass
class Hit:
    chunk: Chunk
    score: float


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TITLE_RE = re.compile(r'^title:\s*"?(.*?)"?\s*$', re.MULTILINE)
_H1_RE = re.compile(r"^# (.+?)$", re.MULTILINE)
# Split on H2 OR H3 — keeps sub-sections like "### Citicorp" as their own chunk
# so BM25 can surface them when a query mentions the issuer name.
_SECTION_SPLIT_RE = re.compile(r"\n(?=##+ )", re.MULTILINE)
_HEADING_PREFIX_RE = re.compile(r"^(##+) ")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Cap on a single chunk's character length. Sections longer than this get
# split on paragraph boundaries so no content is dropped from the prompt.
MAX_CHUNK_CHARS = 1800


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _strip_frontmatter(raw: str) -> tuple[str, dict[str, str]]:
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return raw, {}
    fm_text = m.group(1)
    body = raw[m.end():]
    meta: dict[str, str] = {}
    title_match = _TITLE_RE.search(fm_text)
    if title_match:
        meta["title"] = title_match.group(1).strip()
    return body, meta


def _file_title(body: str, meta: dict[str, str], path: Path) -> str:
    if "title" in meta and meta["title"]:
        return meta["title"]
    h1 = _H1_RE.search(body)
    if h1:
        return h1.group(1).strip()
    return path.stem.replace("-", " ")


def _split_long_section(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """If a section is longer than max_chars, split it on blank-line boundaries
    (preferred) or single-line boundaries (fallback for tables) into roughly
    max_chars-sized pieces."""
    if len(text) <= max_chars:
        return [text]

    def _split_on(separator: str) -> list[str]:
        units = text.split(separator)
        pieces: list[str] = []
        current = ""
        for u in units:
            candidate = u if not current else current + separator + u
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    pieces.append(current)
                # If a single unit is itself oversize, accept it as-is rather
                # than recursing forever.
                current = u
        if current:
            pieces.append(current)
        return pieces

    # Prefer paragraph boundaries; fall back to line boundaries (for tables) if
    # any single paragraph is still oversize.
    pieces = _split_on("\n\n")
    if any(len(p) > max_chars for p in pieces):
        pieces = _split_on("\n")
    return pieces


def _split_into_sections(body: str) -> list[tuple[str, str]]:
    """Return [(section_heading, section_text)]. Splits on H2 and H3 boundaries.
    Long unheaded preambles or oversize sections get sub-split on paragraph
    boundaries to keep every chunk under MAX_CHUNK_CHARS.
    """
    parts = _SECTION_SPLIT_RE.split(body)
    out: list[tuple[str, str]] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = _HEADING_PREFIX_RE.match(part)
        if m:
            head, _, rest = part.partition("\n")
            heading = head[len(m.group(1)) + 1:].strip()
            text = rest.strip()
        else:
            heading = ""
            text = part
        for piece in _split_long_section(text):
            if piece.strip():
                out.append((heading, piece))
    return out


def _iter_md_files(domain_dir: Path) -> Iterable[Path]:
    for p in sorted(domain_dir.rglob("*.md")):
        if p.name == "index.md":
            continue
        yield p


def _load_chunks(domain: str, data_root: Path) -> list[Chunk]:
    domain_dir = data_root / domain
    if not domain_dir.is_dir():
        raise FileNotFoundError(f"Corpus directory not found: {domain_dir}")
    chunks: list[Chunk] = []
    for path in _iter_md_files(domain_dir):
        raw = path.read_text(encoding="utf-8", errors="replace")
        body, meta = _strip_frontmatter(raw)
        title = _file_title(body, meta, path)
        rel_path = str(path.relative_to(data_root))
        sections = _split_into_sections(body)
        if not sections:
            continue
        for section_heading, section_text in sections:
            if not section_text.strip():
                continue
            chunks.append(Chunk(
                domain=domain,
                path=rel_path,
                title=title,
                section=section_heading,
                text=section_text,
            ))
    return chunks


class Retriever:
    """Holds one BM25 index per domain. Build lazily on first query per domain."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self._chunks: dict[str, list[Chunk]] = {}
        self._indexes: dict[str, BM25Okapi] = {}

    def _ensure_index(self, domain: str) -> None:
        if domain in self._indexes:
            return
        if domain not in DOMAINS:
            raise ValueError(f"Unknown domain: {domain}")
        chunks = _load_chunks(domain, self.data_root)
        if not chunks:
            raise RuntimeError(f"No chunks loaded for domain {domain}")
        tokenized = [_tokenize(f"{c.title} {c.section} {c.text}") for c in chunks]
        self._chunks[domain] = chunks
        self._indexes[domain] = BM25Okapi(tokenized)

    def search(self, domain: str, query: str, top_k: int = 5) -> list[Hit]:
        self._ensure_index(domain)
        tokens = _tokenize(query)
        if not tokens:
            return []
        scores = self._indexes[domain].get_scores(tokens)
        chunks = self._chunks[domain]
        ranked = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
        hits: list[Hit] = []
        seen_paths: set[str] = set()
        for score, chunk in ranked:
            if score <= 0:
                break
            if chunk.path in seen_paths:
                continue
            seen_paths.add(chunk.path)
            hits.append(Hit(chunk=chunk, score=float(score)))
            if len(hits) >= top_k:
                break
        return hits

    def search_multi(self, domain: str, queries: list[str], top_k: int = 5) -> list[Hit]:
        """Run several queries against the same domain and merge by max score per chunk."""
        self._ensure_index(domain)
        chunks = self._chunks[domain]
        best: dict[str, tuple[float, Chunk]] = {}
        for q in queries:
            for hit in self.search(domain, q, top_k=top_k):
                key = hit.chunk.path
                if key not in best or hit.score > best[key][0]:
                    best[key] = (hit.score, hit.chunk)
        merged = sorted(best.values(), key=lambda x: x[0], reverse=True)[:top_k]
        return [Hit(chunk=c, score=s) for s, c in merged]

    def canonical_product_areas(self, domain: str) -> list[str]:
        """Return the canonical product_area label set for a domain.

        For HackerRank and Claude, derived from the on-disk folder names with
        aliases applied. For Visa, a small fixed list since the folder
        structure is uninformative. Nested namespace folders (e.g. data/claude/claude/*)
        are walked one level deeper so labels like 'conversation_management' show up.
        """
        if domain == "visa":
            return list(_VISA_LABELS)
        domain_dir = self.data_root / domain
        if not domain_dir.is_dir():
            return []
        labels: list[str] = []
        for sub in sorted(domain_dir.iterdir()):
            if not sub.is_dir():
                continue
            if sub.name == domain:
                # Same name as the parent domain (e.g. data/claude/claude/) -- this
                # is a sub-namespace; surface the children as labels.
                for nested in sorted(sub.iterdir()):
                    if not nested.is_dir():
                        continue
                    label = _normalize_folder(nested.name)
                    if label and label not in labels:
                        labels.append(label)
            else:
                label = _normalize_folder(sub.name)
                if label and label not in labels:
                    labels.append(label)
        return labels


def _normalize_folder(name: str) -> str:
    norm = name.lower().replace("-", "_")
    return _FOLDER_ALIASES.get(norm, norm)


def derive_product_area(hits: list[Hit], top_n: int = 3) -> str:
    """Pick the most common normalized folder among the top-N hits.

    Walks one level deeper when the first subfolder repeats the domain name
    (e.g. data/claude/claude/conversation-management/foo.md -> conversation_management),
    since those are sub-namespaces, not real categories.
    """
    if not hits:
        return ""
    folders: list[str] = []
    for hit in hits[:top_n]:
        parts = hit.chunk.path.split("/")
        if len(parts) < 2:
            continue
        domain = parts[0]
        # parts: [domain, sub1, sub2, ...]. If sub1 == domain, use sub2.
        if len(parts) >= 3 and parts[1] == domain:
            folders.append(_normalize_folder(parts[2]))
        else:
            folders.append(_normalize_folder(parts[1]))
    if not folders:
        return ""
    from collections import Counter
    return Counter(folders).most_common(1)[0][0]


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python -m code.retrieve <domain> <query>")
        sys.exit(1)
    domain = sys.argv[1]
    query = " ".join(sys.argv[2:])
    root = Path(__file__).resolve().parent.parent / "data"
    r = Retriever(root)
    for hit in r.search(domain, query, top_k=5):
        print(f"{hit.score:6.2f}  {hit.chunk.title}  ({hit.chunk.path})")
