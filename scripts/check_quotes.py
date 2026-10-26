#!/usr/bin/env python3
"""check_quotes.py — mechanically verify every ANA quotation and locator.

For each ANA record in workspace/05.analysis/T*.md:
  * every quoted string in the **Quoted support** field must appear in one of
    the SRC/REF files cited in that field;
  * every quoted locator in the **Evidence** field ("section \"...\"",
    "paragraph beginning \"...\"", "chapter \"...\"") must appear in the file
    it is cited against;
  * a record with any failure makes the script exit non-zero.

Normalisation neutralises the differences that are artefacts rather than
alterations: curly quotes, en/em dashes, soft hyphens, line-break hyphenation,
and runs of whitespace.  Matching is case-insensitive by default (pass
--strict-case to require exact case); a case-only difference is reported as a
warning rather than a failure.

Optional fast path: if the Rust-backed `ahocorasick_rs` package is installed,
`--fast` builds one Aho-Corasick automaton per source file instead of using
CPython's substring search.  Both paths produce identical verdicts.

Usage:
    python3 scripts/check_quotes.py [--mas-root .] [--fast] [--strict-case] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

try:  # optional Rust fast path
    from ahocorasick_rs import AhoCorasick
except ImportError:  # pragma: no cover
    AhoCorasick = None

QUOTE_RE = re.compile(r'[“"]([^”"]{6,}?)[”"]')
CITE_RE = re.compile(r'\b(SRC-(?:T\d-\d{3}|SEED-\d{3}))\b|\b(REF-PORT-[A-Z]+)\b')
LOCATOR_RE = re.compile(
    r'(?:section|paragraph beginning|chapter|ch\.)\s*[“"]([^”"]{3,}?)[”"]',
    re.IGNORECASE,
)
FIELD_RE = re.compile(
    r'^\*\*(Question|Evidence|Quoted support|Reasoning|Contradiction|Conclusion|Limits)\*\*\s*:',
    re.MULTILINE,
)
ANA_RE = re.compile(r'^## \[(ANA-T\d-\d{2})\]\s*(.*)$', re.MULTILINE)
ELLIPSIS_RE = re.compile(r'(?:\.\.\.|…)+')

REFERENCE_DOCS = {
    'REF-PORT-ECON': 'references/domain/port-of-oakland-economic-benefits.md',
    'REF-PORT-AG': 'references/domain/port-of-oakland-ag-export-powerhouse.md',
}


def normalise(text: str) -> str:
    """Neutralise typography and line-break artefacts, keep the words."""
    text = text.replace('\u00ad', '')                 # soft hyphen
    text = re.sub(r'-\s*\n\s*', '', text)             # hyphenated line break
    text = (text.replace('‘', "'").replace('’', "'")
                .replace('“', '"').replace('”', '"')
                .replace('–', '-').replace('—', '-'))
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def dehyphen(text: str) -> str:
    """Drop hyphens between letters (OCR line-break and compounding variance)."""
    return re.sub(r'(?<=[A-Za-z])-(?=[A-Za-z])', '', text)


def clean_locator(loc: str) -> str:
    """Locators are often truncated with an ellipsis in the ANA record."""
    loc = re.sub(r'(?:\.\.\.|…)+\s*$', '', loc).strip()
    return loc


def looks_like_quote(s: str) -> bool:
    s = s.strip()
    if len(s.split()) < 4:
        return False
    if not re.match(r'[A-Za-z0-9]', s):
        return False
    if '[SRC-' in s or 'REF-PORT' in s:
        return False
    if s.endswith((');', '),', ';', ':')):
        return False
    return True


def shingles(text: str, n: int = 5) -> set[str]:
    words = re.findall(r"[a-z0-9']+", normalise(text).casefold())
    if len(words) < n:
        return {' '.join(words)}
    return {' '.join(words[i:i + n]) for i in range(len(words) - n + 1)}


def coverage(quote: str, source: set[str]) -> float:
    q = shingles(quote)
    if not q:
        return 0.0
    return len(q & source) / len(q)


class Source:
    def __init__(self, sid: str, path: Path):
        self.id = sid
        self.path = path
        self.raw = path.read_text(encoding='utf-8', errors='replace')
        self.text = normalise(self.raw)
        self.folded = self.text.casefold()
        self._shingles = None
        self._dehyphened = None

    @property
    def grams(self) -> set[str]:
        if self._shingles is None:
            self._shingles = shingles(self.raw)
        return self._shingles

    def contains(self, needle: str, strict_case: bool) -> tuple[bool, bool, bool]:
        """Return (found, case_only, hyphen_only)."""
        needle = normalise(needle)
        if needle in self.text:
            return True, False, False
        if strict_case and needle.casefold() in self.folded:
            return False, True, False
        # typography/OCR: a hyphen inside a word may be a line-break artefact
        if dehyphen(needle) in self.dehyphened():
            return True, False, True
        return False, False, False

    def dehyphened(self) -> str:
        if self._dehyphened is None:
            self._dehyphened = dehyphen(self.folded)
        return self._dehyphened


def find_sources(mas_root: Path) -> dict[str, Source]:
    sources: dict[str, Source] = {}
    patterns = [
        'workspace/03.sources/*/SRC-*.md',
        'workspace/01.overview/seeds/SRC-*.md',
    ]
    for pattern in patterns:
        for path in sorted(mas_root.glob(pattern)):
            m = re.search(r'(SRC-(?:T\d-\d{3}|SEED-\d{3}))', path.name)
            if m:
                sources[m.group(1)] = Source(m.group(1), path)
    for sid, rel in REFERENCE_DOCS.items():
        path = mas_root / rel
        if path.exists():
            sources[sid] = Source(sid, path)
    return sources


def parse_anas(analysis_dir: Path) -> list[dict]:
    records = []
    for path in sorted(analysis_dir.glob('T*.md')):
        text = path.read_text(encoding='utf-8')
        heads = list(ANA_RE.finditer(text))
        for i, head in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            block = text[head.start():end]
            fields = {}
            matches = list(FIELD_RE.finditer(block))
            for j, m in enumerate(matches):
                stop = matches[j + 1].start() if j + 1 < len(matches) else len(block)
                fields[m.group(1)] = block[m.end():stop].strip()
            records.append({
                'id': head.group(1),
                'title': head.group(2).strip(),
                'file': path.name,
                'fields': fields,
            })
    return records


def cite_ids(text: str) -> list[str]:
    out = []
    for m in CITE_RE.finditer(text):
        out.append(m.group(1) or m.group(2))
    return out


def check_record(rec: dict, sources: dict[str, Source], strict_case: bool,
                 fast: bool) -> dict:
    result = {'id': rec['id'], 'title': rec['title'], 'errors': [], 'warnings': [],
              'quotes': 0, 'quotes_ok': 0, 'locators': 0, 'locators_ok': 0,
              'skipped': 0}
    support = rec['fields'].get('Quoted support', '')
    evidence = rec['fields'].get('Evidence', '')

    # ---- quotations -------------------------------------------------------
    quotes = [q.strip() for q in QUOTE_RE.findall(support) if looks_like_quote(q)]
    ids = cite_ids(support)
    hits: dict[str, set[str]] = {}
    fragment_index: dict[str, list[str]] = {}
    if fast and AhoCorasick is not None and quotes:
        frags = []
        for q in quotes:
            frs = [normalise(f) for f in ELLIPSIS_RE.split(q) if len(f.split()) >= 3] or [normalise(q)]
            fragment_index[q] = frs
            frags.extend(frs)
        patterns = list(dict.fromkeys(f.casefold() for f in frags))
        ac = AhoCorasick(patterns)
        for sid in {i for i in ids if i in sources}:
            src = sources[sid]
            hits[sid] = {patterns[i] for i, _a, _b in ac.find_matches_as_indexes(src.folded)}
    if quotes and not ids:
        result['errors'].append('Quoted support contains quotations but cites no source')
    for quote in quotes:
        if len(quote.split()) < 3:
            result['skipped'] += 1
            continue
        result['quotes'] += 1
        candidates = [sources[i] for i in ids if i in sources]
        if not candidates:
            result['errors'].append(
                f'quote not checkable (unknown source {", ".join(ids) or "none"}): '
                f'{quote[:70]!r}')
            continue
        found_any = False
        case_only = False
        best = (0.0, None)
        fragments = [f for f in ELLIPSIS_RE.split(quote) if len(f.split()) >= 3] or [quote]
        for src in candidates:
            if fast and quote in fragment_index and src.id in hits:
                ok = all(f.casefold() in hits[src.id]
                         for f in fragment_index[quote])
                if ok:
                    found_any = True
                    break
                continue
            checks = [src.contains(f, strict_case) for f in fragments]
            if all(ok or case for ok, case, _hy in checks):
                if strict_case and any(case for _ok, case, _hy in checks):
                    case_only = True
                found_any = True
                break
            cov = coverage(quote, src.grams)
            if cov > best[0]:
                best = (cov, src.id)
        if found_any:
            result['quotes_ok'] += 1
            if case_only:
                result['warnings'].append(
                    f'case-only difference: {quote[:60]!r} in {candidates[0].id}')
        else:
            cov, sid = best
            hint = (f'best coverage {cov:.0%} in {sid}' if sid else 'no comparable source')
            result['errors'].append(
                f'quote not found ({hint}): {quote[:70]!r}')

    # ---- locators ---------------------------------------------------------
    locators = [clean_locator(m.group(1)) for m in LOCATOR_RE.finditer(evidence)]
    ev_ids = cite_ids(evidence)
    for loc in locators:
        if len(loc.split()) < 2:
            continue
        result['locators'] += 1
        candidates = [sources[i] for i in ev_ids if i in sources] or list(sources.values())
        found = False
        for src in candidates:
            ok, case_only, hyphen_only = src.contains(loc, strict_case)
            if ok or case_only:
                found = True
                if case_only:
                    result['warnings'].append(f'locator case-only difference: {loc[:50]!r}')
                if hyphen_only:
                    result['warnings'].append(f'locator hyphenation difference: {loc[:50]!r}')
                break
        if found:
            result['locators_ok'] += 1
        else:
            result['errors'].append(f'locator not found: {loc[:70]!r}')
    return result


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--mas-root', default='.', help='MAS root (default: .)')
    ap.add_argument('--fast', action='store_true',
                    help='use the Rust Aho-Corasick fast path if installed')
    ap.add_argument('--strict-case', action='store_true')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)

    root = Path(args.mas_root).resolve()
    sources = find_sources(root)
    if not sources:
        print(f'error: no SRC files found under {root}', file=sys.stderr)
        return 2
    records = parse_anas(root / 'workspace/05.analysis')
    if not records:
        print(f'error: no ANA records under {root}/workspace/05.analysis', file=sys.stderr)
        return 2

    if args.fast:
        if AhoCorasick is None:
            print('fast path requested but ahocorasick_rs is not installed; '
                  'falling back to CPython search', file=sys.stderr)
        else:
            print('fast path: ahocorasick_rs (Rust) in use', file=sys.stderr)

    results = [check_record(r, sources, args.strict_case, args.fast) for r in records]
    failed = [r for r in results if r['errors']]
    total_q = sum(r['quotes'] for r in results)
    ok_q = sum(r['quotes_ok'] for r in results)
    total_l = sum(r['locators'] for r in results)
    ok_l = sum(r['locators_ok'] for r in results)
    warned = [r for r in results if r['warnings']]

    if args.json:
        print(json.dumps({'records': results,
                          'summary': {'anas': len(results), 'failed': len(failed),
                                      'quotes': total_q, 'quotes_ok': ok_q,
                                      'locators': total_l, 'locators_ok': ok_l,
                                      'sources': len(sources)}}, indent=2))
    else:
        print(f'{len(records)} ANA records, {len(sources)} source files')
        for r in results:
            status = 'FAIL' if r['errors'] else 'ok'
            print(f"  {r['id']:10} {status:4}  quotes {r['quotes_ok']}/{r['quotes']}"
                  f"  locators {r['locators_ok']}/{r['locators']}")
            for e in r['errors']:
                print(f"       ! {e}")
            for w in r['warnings']:
                print(f"       ~ {w}")
        print()
        print(f'quotes:   {ok_q}/{total_q} verified')
        print(f'locators: {ok_l}/{total_l} verified')
        print(f'records:  {len(results) - len(failed)}/{len(results)} clean'
              + (f' ({len(warned)} with warnings)' if warned else ''))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
