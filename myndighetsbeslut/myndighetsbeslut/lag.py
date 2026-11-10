"""Statute texts (data.riksdagen.se) and citation handling.

A citation is written the Swedish way, e.g. "3 kap. 18 § studiestödslagen" or
"25 § förvaltningslagen". `norm` turns it into (law, chapter, paragraph) and
`exists` checks that the paragraph is really in the statute text, so that an
invented reference can be told apart from a merely irrelevant one.
"""
import functools
import re
from pathlib import Path

LAG_DIR = Path(__file__).resolve().parent.parent / "sources" / "lag"

LAGAR = {
    "fl": ("sfs-2017-900", "förvaltningslagen", ["förvaltningslag", "fl"]),
    "ssl": ("sfs-1999-1395", "studiestödslagen", ["studiestödslag", "ssl"]),
    "ssf": ("sfs-2000-655", "studiestödsförordningen", ["studiestödsförordning", "ssf"]),
    "skl": ("sfs-2010-800", "skollagen", ["skollag", "skl"]),
}

_KAP = re.compile(r"^(\d+ ?[a-z]?) kap\.")
_PAR = re.compile(r"^(\d+ ?[a-z]?) § ")
_REF = re.compile(r"(?:(\d+)\s*([a-z])?\s*kap\.?\s*)?(\d+)\s*([a-z])?\s*§+\s*(?:\S+\s+stycket\s+)?(?:\d\s+)?([A-Za-zåäöÅÄÖ-]+)?")


@functools.lru_cache(maxsize=None)
def index(law):
    """Set of (chapter, paragraph) present in the statute; chapter '' if none."""
    path = LAG_DIR / (LAGAR[law][0] + ".text")
    out, kap, prev_blank = set(), "", True
    for line in path.read_text(encoding="utf-8").replace("\r", "").split("\n"):
        s = line.strip()
        if s.startswith("Övergångsbestämmelser"):
            break
        s = re.sub(r"^/[^/]*/\s*", "", s)      # "/Träder i kraft I:2027-01-01/" markers
        if prev_blank:                           # headings and paragraphs start after a blank line
            m = _KAP.match(s)
            if m and len(s) < 120 and "§" not in s:
                kap = m.group(1).replace(" ", "")
            else:
                m = _PAR.match(s)
                if m:
                    out.add((kap, m.group(1).replace(" ", "")))
        prev_blank = not s
    return out


def _law_of(word):
    w = (word or "").lower()
    for key, (_, name, stems) in LAGAR.items():
        if any(w.startswith(s) for s in stems if len(s) > 3) or w in stems:
            return key
    return None


def norm(ref):
    """'3 kap. 18 § första stycket studiestödslagen' -> ('ssl', '3', '18'), or None."""
    if not isinstance(ref, str):
        return None
    text = ref.strip()
    law = None
    for key, (_, name, stems) in LAGAR.items():
        low = text.lower()
        if any(s in low for s in stems if len(s) > 3) or re.search(r"\b%s\b" % stems[-1], low):
            law = key
            break
    m = re.search(r"(?:(\d+)\s*([a-z])?\s*kap\.?\s*)?(\d+)\s*([a-z])?\s*§", text)
    if not m or not law:
        return None
    kap = (m.group(1) or "") + (m.group(2) or "")
    par = m.group(3) + (m.group(4) or "")
    return (law, kap, par)


def fmt(t):
    law, kap, par = t
    return ("%s kap. " % kap if kap else "") + "%s § %s" % (par, LAGAR[law][1])


def exists(t):
    return t is not None and (t[1], t[2]) in index(t[0])


def score_refs(pred, truth):
    """F1 over normalised references; any reference to a paragraph that does not
    exist in the statute gives 0."""
    p = [norm(x) for x in (pred or [])]
    if any(x is None or not exists(x) for x in p):
        return 0.0
    p, t = set(p), {norm(x) for x in truth}
    if not p and not t:
        return 1.0
    tp = len(p & t)
    if not tp:
        return 0.0
    pr, rc = tp / len(p), tp / len(t)
    return 2 * pr * rc / (pr + rc)
