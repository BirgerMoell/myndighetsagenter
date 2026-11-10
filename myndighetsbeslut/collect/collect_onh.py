#!/usr/bin/env python3
"""Collect published decisions ("Avgoranden") from Overklagandenamnden for
hogskolan (ONH), https://www.onh.se/.

How the site publishes decisions
--------------------------------
* SiteVision site. robots.txt (User-agent: *) only disallows technical URLs
  (*.html.printable, ?sv.state=, ;jsessionid=, *.pdf?properties= ...); the
  decision pages and the listing are not disallowed. There is no sitemap.xml.
* https://www.onh.se/avgoranden/avgoranden is a small search app. It shows 20
  hits and a "Visa fler" button; the button makes a read-only search request
      POST /avgoranden/avgoranden?sv.target=<portlet>&sv.<portlet>.route=/search
      body {"page": N, "query": "", "filter": []}
  which returns JSON {searchHits: [{title, preamble, uri, date, type}],
  totalHits}. This collector makes exactly that request to enumerate hits.
* Every decision is its own HTML page (no PDFs):
      category tag, <h1>Beslut YYYY-MM-DD (reg.nr NN-NNNN-YY)</h1>,
      a preamble (summary) and a body whose section headings are bold
      paragraphs (Bakgrund / Yrkande m.m. / Skal / Namndens beslut ...).

Behaviour
---------
* Generic User-Agent, >= 1 s between requests, strictly sequential.
* Raw files cached under data/raw/onh/{listing,pages}; cached files are never
  re-fetched, so the run is resumable (and re-parsing is offline).
* One document is fetched, parsed and written at a time.
* Stops immediately on HTTP 403/429 or anything that looks like a block page.

Usage:  python3 collect_onh.py --limit 1500
Python 3.9, standard library only.
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "https://www.onh.se"
LIST_URL = BASE + "/avgoranden/avgoranden"
PORTLET = "12.5e92e42e181262dec29247a6"
SEARCH_URL = (LIST_URL + "?sv.target=" + PORTLET + "&sv." + PORTLET
              + ".route=%2Fsearch")
UA = "Mozilla/5.0 (compatible; research-collector/1.0)"
DELAY = 1.2
MAX_BYTES = 1024 ** 3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw", "onh")
OUT = os.path.join(ROOT, "data", "onh_decisions.jsonl")

_last = [0.0]
_bytes = [0]


class Blocked(Exception):
    pass


def fetch(url, data=None):
    """One polite request. data=None -> GET, else JSON POST (search only)."""
    wait = DELAY - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    headers = {"User-Agent": UA, "Accept-Language": "sv"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers.update({"Content-Type": "application/json",
                        "Accept": "application/json",
                        "X-Requested-With": "XMLHttpRequest"})
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        _last[0] = time.time()
        if e.code in (403, 429, 503):
            raise Blocked("HTTP %d for %s" % (e.code, url))
        raise
    _last[0] = time.time()
    _bytes[0] += len(raw)
    if _bytes[0] > MAX_BYTES:
        raise Blocked("download budget of 1 GB exceeded")
    low = raw[:20000].lower()
    if b"captcha" in low or b"access denied" in low:
        raise Blocked("block/captcha page for %s" % url)
    return raw


# ---------------------------------------------------------------- listing

def listing_pages():
    """Yield search hits page by page (cached)."""
    d = os.path.join(RAW, "listing")
    os.makedirs(d, exist_ok=True)
    page, seen, total = 0, 0, None
    while total is None or seen < total:
        p = os.path.join(d, "page_%03d.json" % page)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                res = json.load(f)
        else:
            raw = fetch(SEARCH_URL, {"page": page, "query": "", "filter": []})
            res = json.loads(raw.decode("utf-8"))
            if "searchHits" not in res:
                raise Blocked("unexpected search response on page %d" % page)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False)
        hits = res.get("searchHits") or []
        total = res.get("totalHits", 0)
        if not hits:
            break
        for h in hits:
            yield h, total
        seen += len(hits)
        page += 1


def hit_date(h):
    m = re.search(r"(\d{4}-\d{2})-\s*(\d{2})", h.get("title", ""))
    if m:
        return m.group(1) + "-" + m.group(2)
    m = re.search(r"beslut-(\d{4}-\d{2})-+(\d{2})", h.get("uri", ""))
    if m:
        return m.group(1) + "-" + m.group(2)
    m = re.search(r"/(\d{4}-\d{2}-\d{2})-", h.get("uri", ""))
    return m.group(1) if m else ""


# ---------------------------------------------------------------- parsing

def clean(s):
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ").replace("­", "")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\s*\n\s*", "\n", s).strip()


BLOCK_RE = re.compile(r"<(p|h[1-6]|li|td|th)\b[^>]*>(.*?)</\1>", re.S)
BOLD_ONLY_RE = re.compile(
    r"^\s*(?:<(?:strong|b|em|i|span)\b[^>]*>\s*)+(.*?)(?:\s*</(?:strong|b|em|i|span)>)+\s*$",
    re.S)

KNOWN_HEADINGS = re.compile(
    r"^(bakgrund|ärendet|saken|yrkanden?( m\.? ?m\.?)?|yrkande och grunder|"
    r"överklagandet|skäl|skälen för( nämndens)? beslut(et)?|skäl för beslutet|"
    r"(överklagande)?nämndens (bedömning|beslut|skäl)|beslut|"
    r"överklagandenämndens (bedömning|beslut|skäl)|bedömning|"
    r"tillämpliga bestämmelser|rättslig reglering|gällande bestämmelser|"
    r"bestämmelser|utredningen|skiljaktig mening|särskilt yttrande)\s*:?$", re.I)


def body_blocks(page):
    """Return (area, title, summary, published, [(is_heading, text), ...])."""
    m = re.search(r'hiq-decision-tag__tag-text">\s*([^<]+?)\s*<', page)
    area = html.unescape(m.group(1)) if m else ""
    m = (re.search(r'<h1 class="hiq-h1"[^>]*>(.*?)</h1>', page, re.S)
         or re.search(r"<!-- Rubrik -->.*?<h1[^>]*>(.*?)</h1>", page, re.S))
    title = clean(m.group(1)) if m else ""
    m = re.search(r'<p class="hiq-preamble">(.*?)</p>', page, re.S)
    summary = clean(m.group(1)) if m else ""
    m = re.search(r"Sidan (?:publicerades|uppdaterades) den ([^<]+)<", page)
    published = m.group(1).strip() if m else ""

    i = page.find("<!-- Brödtext -->")
    if i < 0:
        i = page.find("</h1>")
    j = page.find("<!-- Dela/skriv ut", i)
    if j < 0:
        j = page.find("</main>", i)
    body = page[i:j]
    blocks = []
    for tag, inner in BLOCK_RE.findall(body):
        if "hiq-preamble" in inner:
            continue
        text = clean(inner)
        if not text:
            continue
        is_head = False
        if tag in ("h2", "h3", "h4", "h5"):
            is_head = True
        elif tag == "p" and len(text) < 90:
            if BOLD_ONLY_RE.match(inner) and not text.endswith("."):
                is_head = True
            elif KNOWN_HEADINGS.match(text):
                is_head = True
        blocks.append((is_head, text))
    return area, title, summary, published, blocks


UNI_NAMES = [
    "Uppsala universitet", "Lunds universitet", "Göteborgs universitet",
    "Stockholms universitet", "Umeå universitet", "Linköpings universitet",
    "Karolinska institutet", "Kungl. Tekniska högskolan",
    "Kungliga Tekniska högskolan", "Tekniska högskolan i Stockholm", "KTH",
    "Chalmers tekniska högskola", "Luleå tekniska universitet",
    "Sveriges lantbruksuniversitet", "Karlstads universitet",
    "Linnéuniversitetet", "Örebro universitet", "Mittuniversitetet",
    "Mitthögskolan", "Malmö universitet", "Malmö högskola",
    "Mälardalens universitet", "Mälardalens högskola",
    "Blekinge tekniska högskola", "Försvarshögskolan",
    "Gymnastik- och idrottshögskolan", "Högskolan i Borås",
    "Högskolan Dalarna", "Högskolan i Gävle", "Högskolan i Halmstad",
    "Högskolan Kristianstad", "Högskolan i Kristianstad",
    "Högskolan i Skövde", "Högskolan Väst", "Högskolan i Trollhättan/Uddevalla",
    "Konstfack", "Kungl. Konsthögskolan", "Kungl. Musikhögskolan",
    "Kungliga Musikhögskolan", "Stockholms konstnärliga högskola",
    "Södertörns högskola", "Högskolan i Jönköping", "Jönköping University",
    "Stiftelsen Högskolan i Jönköping", "Handelshögskolan i Stockholm",
    "Högskolan på Gotland", "Högskolan i Kalmar", "Växjö universitet",
    "Lärarhögskolan i Stockholm", "Stockholms dramatiska högskola",
    "Dans- och cirkushögskolan", "Dans och Cirkushögskolan", "Operahögskolan",
    "Marie Cederschiöld högskola", "Ersta Sköndal Bräcke högskola",
    "Ersta Sköndal högskola", "Sophiahemmet Högskola", "Röda Korsets Högskola",
    "Universitets- och högskolerådet", "Universitets och högskolerådet",
    "Verket för högskoleservice", "Högskoleverket", "Vetenskapsrådet",
    "Myndigheten för yrkeshögskolan",
]
UNI_CANON = {
    "Kungliga Tekniska högskolan": "Kungl. Tekniska högskolan",
    "Tekniska högskolan i Stockholm": "Kungl. Tekniska högskolan",
    "KTH": "Kungl. Tekniska högskolan",
    "Kungliga Musikhögskolan": "Kungl. Musikhögskolan",
    "Universitets och högskolerådet": "Universitets- och högskolerådet",
    "Stiftelsen Högskolan i Jönköping": "Högskolan i Jönköping",
    "Dans och Cirkushögskolan": "Dans- och cirkushögskolan",
    "Högskolan i Kristianstad": "Högskolan Kristianstad",
}


def _uni_regex(name):
    # tolerate genitive -s and dropped definite -et ("Linnéuniversitet")
    pat = re.escape(name)
    if name.endswith("universitetet"):
        pat = re.escape(name[:-2]) + r"(?:et)?"
    return re.compile(r"(?<![\w-])" + pat + r"s?(?![\w])",
                      0 if name == "KTH" else re.I)


UNI_RES = [(n, _uni_regex(n)) for n in UNI_NAMES]
UNI_GENERIC = re.compile(
    r"\b([A-ZÅÄÖ][\wåäöé\-]+ (?:tekniska )?(?:universitet|högskola))\b")
NOT_HEI = ("Universitets- och högskolerådet", "Verket för högskoleservice",
           "Högskoleverket", "Vetenskapsrådet", "Myndigheten för yrkeshögskolan")


def find_university(text):
    """Institution whose decision was appealed: the first higher-education
    institution named in the text. UHR/VHS are only used when no institution
    is named at all (they administer admission on the institutions' behalf)."""
    best, fallback = None, None
    for name, rx in UNI_RES:
        m = rx.search(text)
        if not m:
            continue
        name = UNI_CANON.get(name, name)
        if name in NOT_HEI:
            if fallback is None or m.start() < fallback[0]:
                fallback = (m.start(), name)
        elif best is None or m.start() < best[0]:
            best = (m.start(), name)
    if best:
        return best[1]
    m = UNI_GENERIC.search(text)
    if m and not m.group(1).startswith(("Vid ", "Ett ", "En ")):
        return m.group(1)
    return fallback[1] if fallback else None


STATUTE = (r"(?:högskoleförordningen|högskolelagen|förvaltningslagen|"
           r"diskrimineringslagen|anställningsförordningen|regeringsformen|"
           r"lagen om offentlig anställning|studiestödslagen|"
           r"[a-zåäö]+(?:förordningen|lagen|balken)|"
           r"förordningen \(\d{4}:\d+\)[^.;,)]{0,80}?|"
           r"lagen \(\d{4}:\d+\)[^.;,)]{0,80}?)")
PROV_RE = re.compile(
    r"((?:\d+\s*kap\.\s*)?\d+\s*[a-z]?(?:\s*(?:–|-|och|eller|,)\s*\d+\s*[a-z]?)*\s*§§?"
    r"(?:\s*(?:första|andra|tredje|fjärde|femte|sjätte)\s+stycket)?(?:\s*\d+)?"
    r"(?:\s+i)?\s+" + STATUTE + r"(?:\s*\(\d{4}:\d+\))?)")
BILAGA_RE = re.compile(r"(bilaga \d+ till " + STATUTE + r"(?:\s*\(\d{4}:\d+\))?)", re.I)


def find_provisions(text):
    out = []
    for rx in (PROV_RE, BILAGA_RE):
        for m in rx.finditer(text):
            p = re.sub(r"\s+", " ", m.group(1)).strip()
            if p not in out:
                out.append(p)
    return out


BOARD = r"(?:Överklagandenämnden(?: för högskolan)?|Nämnden|ÖNH)"
OPERATIVE_VERBS = (r"bifaller|avslår|avvisar|undanröjer|upphäver|återförvisar|"
                   r"visar|skriver|avskriver|lämnar|förklarar|ändrar|beslutar|"
                   r"tar inte upp|fastställer|överlämnar|anställer|finner")
OPER_RE = re.compile(r"^" + BOARD + r"\s+(?:" + OPERATIVE_VERBS + r")\b", re.I)
DECISION_HEAD = re.compile(
    r"^((överklagande)?nämndens (beslut|avgörande)|beslut|avgörande)\s*:?$", re.I)
ANNEX_HEAD = re.compile(r"^(bilag|skiljaktig|särskilt yttrande|avvikande)", re.I)
CONCL_RE = re.compile(
    r"Överklagande(?:t|na) (?:ska|skall|bör|kan)(?: \w+){0,3}? "
    r"(?:bifallas|avslås|avvisas|vinna bifall|lämnas utan bifall)|"
    r"Ärendet (?:avskrevs|skrevs av)")
# paragraphs after the operative part: appeal notice, reservations, obiter
TRAILER_RE = re.compile(
    r"inte överklagas|reserverade sig|skiljaktig mening|särskilt yttrande|"
    r"^Med anledning av ärendet|^[.\- ]{4,}$|^Beslutet gäller|"
    r"^I (?:beslutet|avgörandet) (?:har )?deltog|^Det ankommer på", re.I)
NO_APPEAL_RE = re.compile(r"Enligt .{0,160}?inte överklagas\.?")
ANCILLARY_RE = re.compile(
    r"[^.\n]*\bavvisar\b[^.\n]*\b(?:yrkande\w*|begäran)\b[^.\n]*?"
    r"(?:\.|\n|$| och (?=avslår|bifaller|undanröjer|upphäver))")


def split_sentences(t):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-ZÅÄÖ])", t) if s.strip()]


def find_outcome_raw(sections, order, blocks):
    """Operative sentence(s): the 'Nämndens beslut' section if the decision
    has one, else trailing sentences where the board itself is the subject of
    an operative verb."""
    for h in reversed(order):
        if DECISION_HEAD.match(h) and sections.get(h):
            keep = []
            body = NO_APPEAL_RE.sub("\n", sections[h])
            for para in body.split("\n"):
                if TRAILER_RE.search(para):
                    break
                keep.append(para)
            return "\n".join(keep).strip(), "section"
    # no decision section: only look at the board's own text, not at annexes
    # or dissenting opinions
    paras = []
    for is_h, t in blocks:
        if is_h and ANNEX_HEAD.match(t):
            break
        if not is_h:
            paras.append(t)
    for t in reversed(paras[-6:]):
        sents = [s for s in split_sentences(t) if OPER_RE.match(s)
                 and not re.match(r"^\S+(?: för högskolan)? finner\b", s)]
        if sents:
            return " ".join(sents), "sentence"
    for t in reversed(paras[-4:]):
        sents = [s for s in split_sentences(t) if CONCL_RE.search(s)]
        if sents:
            return sents[-1], "conclusion"
    return "", None


def classify(raw):
    """Conservative mapping of the operative text to an outcome label."""
    if not raw:
        return "unknown"
    low = raw.lower()
    # dismissal of an ancillary claim (damages, compensation ...) does not
    # decide the label when the appeal itself gets another disposition
    stripped = ANCILLARY_RE.sub(" ", low)
    if re.search(r"bifall|avslå|undanröj|upphäv|åter|avvisa", stripped):
        low = stripped
    t = " ".join(low.split())
    bifall = bool(re.search(
        r"\b(?:bifaller|biföll|bifalls|bifallas|med bifall till)\b", t))
    no_bifall = bool(re.search(
        r"utan bifall|bifaller inte|inte (?:\w+ )?bifall|inte bifallas", t))
    avslag = bool(re.search(r"\bavslår\b|\bavslås\b|\bavslog\b", t)) or no_bifall
    avvis = bool(re.search(r"\bavvisar\b|\bavvisas\b|\bavvisade\b|tar inte upp", t))
    avskr = bool(re.search(r"skriver av|avskriver|avskrivs|skrivs av|avskrev|skrev av", t))
    upph = bool(re.search(r"\bundanröj(?:er|s|de)\b|\bupphäv(?:er|s|de)\b", t))
    ater = bool(re.search(r"visar (?:ärendet |målet |det )?åter|återförvisa|"
                          r"åter till|visas åter|visade (?:ärendet )?åter|"
                          r"(?:lämnar|överlämnar) (?:över )?ärendet till", t))
    delvis = bool(re.search(r"\bdelvis\b|i övrigt|på så sätt|i denna del|"
                            r"i den delen|såvitt avser", t))
    changes = bool(re.search(
        r"\bförklarar\b.*\b(behörig|berättigad|rätt)|\bändrar\b|"
        r"\bskall? (?:tillgodoräknas?|få|ges|antas|utfärda|beviljas?|anställas|förordnas)\b|"
        r"\b(?:beviljar|medger|anställer|tillgodoräknar)\b|"
        r"\bbeslutar att .* ska\b", t))

    if bifall and no_bifall:
        bifall = False
    if bifall and (avslag or avvis):
        return "delvis_bifall"
    if bifall:
        return "delvis_bifall" if re.search(r"\bdelvis\b", t) else "bifall"
    if avvis and not avslag and upph and ater:
        # an ancillary claim is dismissed, the decision itself is sent back
        return "aterforvisning"
    if (avslag or avvis) and (upph or changes):
        return "delvis_bifall"
    if avslag and avvis:
        # one claim dismissed, the appeal otherwise tried and rejected
        return "avslag"
    if avslag and not (upph or ater):
        return "avslag"
    if avvis and not (upph or ater):
        return "avvisning"
    if avskr and not (upph or ater or avslag or avvis):
        return "avskrivning"
    if upph and ater and not (avslag or avvis):
        return "aterforvisning"
    if ater and not (avslag or avvis or upph):
        return "aterforvisning"
    if (upph or changes) and not (avslag or avvis or ater):
        return "bifall"
    return "unknown"


ANON_RE = re.compile(
    r"\b(?:AA|BB|CC|DD|EE|NN|XX|YY|A\.A\.|N\.N\.|N N)\b|"
    r"\b[A-ZÅÄÖ]{1,3}(?::s)? (?:överklaga|ansökte|sökte|yrka|anför|har |är |var |"
    r"uppfyll|ska |skall |får |antogs|begärde|inte )")
TITLES = (r"(?:professorn?|docent(?:en)?|universitetslektorn?|lektorn?|"
          r"doktorand(?:en)?|rektorn?|dekan(?:us|en)?|prefekt(?:en)?|studenten|"
          r"sökanden?|klaganden?|fil\.? ?dr\.?|med\.? ?dr\.?|tekn\.? ?dr\.?|"
          r"ordförande(?:n)?|ledamoten|ledamöterna|advokat(?:en)?|jur\.? ?kand\.?)")
NAME = r"[A-ZÅÄÖÉ][a-zåäöéèü]+(?:[- ][A-ZÅÄÖÉ][a-zåäöéèü]+){1,2}"
NAMED_RE = re.compile(
    r"(?:" + TITLES + r"\s+(" + NAME + r"))|"
    r"(?:(" + NAME + r")\s+(?:överklagar|överklagade|har överklagat|yrkar|"
    r"anför|anförde|ansökte|sökte|anställdes|har anställts|förklarades|"
    r"vidhåller|har inkommit))")
NAME_STOP = re.compile(
    r"universitet|högskola|institutet|nämnden|rådet|verket|sverige|"
    r"styrelsen|fakultet|institution|förordning|lagen|domstol|kammarrätt|"
    r"myndighet|ombudsman|konstfack|chalmers|kungl|europe|stockholm|"
    r"göteborg|uppsala|lund|umeå|karolinska|centrum|program|avdelning|"
    r"regering|riksdag|skolan|enheten|kansli|tekniska|unionen|bolaget|"
    r"^(?:vad|när|om|att|den|det|enligt|av|vid|som|efter|sedan) ", re.I)


def names_persons(text):
    """True if a private individual seems to be named in full, False if the
    parties are only referred to by anonymised initials, else None."""
    for m in NAMED_RE.finditer(text):
        nm = m.group(1) or m.group(2)
        if nm and not NAME_STOP.search(nm):
            return True
    if ANON_RE.search(text):
        return False
    return None


def parse(page, url, hit):
    area, title, summary, published, blocks = body_blocks(page)
    sections, order, cur, pre = {}, [], None, []
    for is_h, text in blocks:
        if is_h:
            cur = text.rstrip(":").strip()
            k, n = cur, 2
            while k in sections:
                k = "%s (%d)" % (cur, n)
                n += 1
            cur = k
            sections[cur] = ""
            order.append(cur)
        elif cur is None:
            pre.append(text)
        else:
            sections[cur] = (sections[cur] + "\n" + text).strip()
    if pre and sections:
        sections = dict([("_inledning", "\n".join(pre))] + list(sections.items()))
    text = "\n".join(("\n" + t + "\n") if is_h else t for is_h, t in blocks).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)

    title = title or hit.get("title", "")
    m = re.search(r"reg\.? ?nr\.?\s*([\d][^)]*\d)", title, re.I)
    dnr = re.sub(r"\s+", " ", m.group(1)).strip() if m else None
    if not dnr:
        m = re.search(r"(?:reg\.?-?nr\.?-|beslut-[\d-]{10,11}-)(\d[\w-]*\d)$", url)
        dnr = m.group(1) if m else None
    raw, how = find_outcome_raw(sections, order, blocks)
    return {
        "source": "onh",
        "url": url,
        "diarienummer": dnr,
        "date": hit_date({"title": title, "uri": url}) or None,
        "title": title,
        "summary": summary or hit.get("preamble", ""),
        "area": (area or hit.get("type", "")).lower() or None,
        "university": find_university(text),
        "provisions": find_provisions(text),
        "text": text,
        "sections": sections,
        "outcome_raw": raw,
        "outcome": classify(raw),
        "names_persons": names_persons(text),
        "published": published or None,
    }


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1500)
    args = ap.parse_args()
    pages_dir = os.path.join(RAW, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    try:
        hits, total, seen = [], 0, set()
        for h, total in listing_pages():
            if h.get("uri") and h["uri"] not in seen:
                seen.add(h["uri"])
                hits.append({k: h.get(k, "") for k in
                             ("title", "preamble", "uri", "type")})
        print("listing: %d unique hits (site reports %d)" % (len(hits), total),
              file=sys.stderr)
        hits.sort(key=hit_date, reverse=True)
        hits = hits[:args.limit]

        tmp = OUT + ".tmp"
        n = 0
        done = set()  # the same decision can be published under two URLs
        with open(tmp, "w", encoding="utf-8") as out:
            for h in hits:
                url = BASE + h["uri"]
                slug = re.sub(r"[^A-Za-z0-9._-]+", "_", h["uri"].rsplit("/", 1)[-1])
                p = os.path.join(pages_dir, slug + ".html")
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as f:
                        page = f.read()
                else:
                    try:
                        page = fetch(url).decode("utf-8", "replace")
                    except urllib.error.HTTPError as e:
                        print("skip %s (HTTP %d)" % (url, e.code), file=sys.stderr)
                        continue
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(page)
                rec = parse(page, url, h)
                if len(rec["text"]) < 200:
                    print("short text: %s" % url, file=sys.stderr)
                key = (rec["diarienummer"], rec["date"])
                if rec["diarienummer"] and key in done:
                    print("duplicate skipped: %s" % url, file=sys.stderr)
                    continue
                done.add(key)
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
                if n % 25 == 0:
                    print("%d/%d" % (n, len(hits)), file=sys.stderr)
        os.replace(tmp, OUT)
        print("wrote %d records to %s (%.1f MB downloaded this run)"
              % (n, OUT, _bytes[0] / 1e6), file=sys.stderr)
    except Blocked as e:
        print("STOPPED: %s" % e, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
