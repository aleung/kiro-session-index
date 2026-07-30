"""Text transforms for kiro-session-index.

FTS5's unicode61 tokenizer treats a whole CJK run as one token, so Chinese is
unsearchable without help. We split CJK into single characters at index time and
apply the same split at query time, wrapping each run in a phrase. Phrase queries
require detail=full (the FTS5 default) -- detail=column silently rejects them.

The one trap worth spelling out: transforming an ENTIRE multi-word query into a
single phrase is wrong, and it fails silently by returning zero rows. Measured on
the real corpus: '记忆 memory' as one phrase found 0 rows; as per-term AND, 8 rows.
build_match() therefore transforms each term independently and joins with AND.
"""

import re

# Ranges cover CJK Unified Ideographs (+ext A), kana, and compatibility ideographs.
CJK_CLASS = r"\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uf900-\ufaff"
_CJK_CHAR = re.compile(f"([{CJK_CLASS}])")
_HAS_CJK = re.compile(f"[{CJK_CLASS}]")
# Split on whitespace, but keep "quoted groups" together.
_TERM = re.compile(r'"([^"]*)"|(\S+)')


def has_cjk(text: str) -> bool:
    return bool(text) and bool(_HAS_CJK.search(text))


def split_index(text: str) -> str:
    """Index-time transform: surround every CJK character with spaces.

    ASCII is untouched, so code and English tokenize normally.
    """
    if not text:
        return ""
    return _CJK_CHAR.sub(r" \1 ", text)


def _fts_quote(s: str) -> str:
    """Wrap in an FTS5 string literal, doubling embedded quotes."""
    return '"' + s.replace('"', '""') + '"'


def _term_to_match(term: str, phrase: bool = False) -> str:
    """Transform one user term into an FTS5 expression.

    A term containing CJK becomes a phrase of its split characters, which gives
    substring semantics within a CJK run ('忘机' matches '遗忘机制') -- what CJK
    users expect. A trailing '*' is preserved as a prefix match.
    """
    prefix = False
    if not phrase and term.endswith("*") and len(term) > 1:
        term, prefix = term[:-1], True

    if has_cjk(term):
        body = " ".join(split_index(term).split())
    else:
        body = term.strip()
    if not body:
        return ""
    return _fts_quote(body) + ("*" if prefix else "")


def build_match(query: str) -> str:
    """Turn a human query into an FTS5 MATCH expression.

    Supported syntax, deliberately small and predictable:
      foo bar        -> both must appear (AND)
      "foo bar"      -> exact phrase
      foo*           -> prefix match
      -foo           -> must NOT appear

    Raises ValueError on an empty query rather than returning something that
    matches everything.
    """
    parts, negated = [], []
    for m in _TERM.finditer(query or ""):
        quoted, bare = m.group(1), m.group(2)
        if quoted is not None:
            expr = _term_to_match(quoted, phrase=True)
            if expr:
                parts.append(expr)
            continue
        term = bare
        neg = term.startswith("-") and len(term) > 1
        if neg:
            term = term[1:]
        expr = _term_to_match(term)
        if not expr:
            continue
        (negated if neg else parts).append(expr)

    if not parts:
        raise ValueError(f"empty or unusable query: {query!r}")
    out = " AND ".join(parts)
    for n in negated:
        out += f" NOT {n}"
    return out


def query_terms(query: str) -> list:
    """Raw (untransformed) terms, used to locate matches in original text."""
    out = []
    for m in _TERM.finditer(query or ""):
        t = m.group(1) if m.group(1) is not None else m.group(2)
        if not t:
            continue
        if t.startswith("-") and len(t) > 1:
            continue
        out.append(t.rstrip("*"))
    return out


def snip(text: str, terms, width: int = 40) -> str:
    """Build a readable snippet from ORIGINAL text.

    This exists because FTS5's snippet() cannot be used here, for two independent
    reasons: on a contentless table it silently returns an empty string, and on a
    normal table it would return the SPLIT text, rendering Chinese as
    '知  识  库' -- unreadable. Slicing the original is the only way to get a
    readable CJK snippet.
    """
    if not text:
        return ""
    if isinstance(terms, str):
        terms = [terms]
    flat = text.replace("\n", " ").replace("\r", " ")
    low = flat.lower()
    pos, hit = -1, ""
    for t in terms or []:
        if not t:
            continue
        i = low.find(t.lower())
        if i >= 0 and (pos < 0 or i < pos):
            pos, hit = i, t
    if pos < 0:
        head = flat[: width * 2].strip()
        return head + ("…" if len(flat) > width * 2 else "")
    start = max(0, pos - width)
    end = min(len(flat), pos + len(hit) + width)
    return ("…" if start > 0 else "") + flat[start:end].strip() + ("…" if end < len(flat) else "")
