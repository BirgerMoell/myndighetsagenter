#!/usr/bin/env python3
"""Collect published, anonymised guiding decisions ("Avgoranden") from
Overklagandenamnden for studiestod (OKS), https://www.oks.se/.

How the site publishes decisions
--------------------------------
* WordPress site. robots.txt allows everything and points to a Yoast sitemap.
* https://www.oks.se/page-sitemap.xml lists all pages; the pages under
  /avgoranden/ are a tree of subject categories. Leaf pages list decisions as
      <h2>Beslut YYYY-MM-DD/Dnr.YYYY-NNNNN</h2>
      <p>one-line summary</p>
      <p><a href=".../wp-content/uploads/YYYY/MM/YYYY-NNNNN.pdf">Dokument: ...</a></p>
      <p class="tiny">Publicerad: YYYY-MM-DD</p>
* The full decision exists only as a PDF; text is extracted with pdftotext.

Behaviour
---------
* Only plain GET requests, generic User-Agent, >= 1 s between requests.
* Raw files cached under data/raw/oks/{pages,pdf,txt}; cached files are never
  re-fetched, so the run is resumable.
* Records are written one at a time to data/oks_decisions.jsonl.
* Stops immediately on HTTP 403/429 or anything that looks like a block page.

Usage:  python3 collect_oks.py --limit 150
Python 3.9, standard library only (plus the `pdftotext` binary).
"""
import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.oks.se"
SITEMAP = BASE + "/page-sitemap.xml"
USER_AGENT = "Mozilla/5.0 (compatible; research-collector/1.0)"
MIN_DELAY = 1.2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw", "oks")
OUT = os.path.join(ROOT, "data", "oks_decisions.jsonl")

_last_request = [0.0]


class Blocked(Exception):
    pass


def fetch(url, path, binary=False):
    """GET url into path (cache). Returns True if a network request was made."""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return False
    wait = MIN_DELAY - (time.time() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read(50 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        _last_request[0] = time.time()
        if e.code in (401, 403, 429, 503):
            raise Blocked("HTTP %d for %s" % (e.code, url))
        raise
    _last_request[0] = time.time()
    if not binary:
        low = data[:20000].lower()
        if b"captcha" in low or b"access denied" in low:
            raise Blocked("block/CAPTCHA page for %s" % url)
    elif not data.startswith(b"%PDF"):
        raise Blocked("expected PDF, got something else for %s" % url)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return True


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


# ---------------------------------------------------------------- listing --

ENTRY_RE = re.compile(
    r"<h2[^>]*>(?P<head>.*?)</h2>(?P<body>.*?)(?=<h2|<hr|</article|\Z)", re.S)
HEAD_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s*/?\s*Dnr\.?\s*:?\s*([0-9]{4}\s*-\s*[0-9]+)", re.I)


def category_pages():
    os.makedirs(os.path.join(RAW, "pages"), exist_ok=True)
    sm = os.path.join(RAW, "pages", "_page-sitemap.xml")
    fetch(SITEMAP, sm)
    urls = re.findall(r"<loc>([^<]+)</loc>", read(sm))
    skip = ("/other-language", "/om-", "/kontakta", "/att-overklaga",
            "/site-map", "/anpassa", "/faq", "/cookies")
    return sorted(u for u in urls
                  if u.rstrip("/") != BASE and not any(s in u for s in skip))


def parse_listing(url, page):
    crumbs = re.search(r'<p id="breadcrumbs">(.*?)</p>', page, re.S)
    area = ""
    if crumbs:
        parts = [p.strip() for p in strip_tags(crumbs.group(1)).split("»")]
        parts = [p for p in parts if p and p not in ("Hem", "Avgöranden")]
        area = " / ".join(parts)
    i = page.find("entry-content")
    j = page.find("</article", i)
    body = page[i:j] if i >= 0 else ""
    for m in ENTRY_RE.finditer(body):
        head = strip_tags(m.group("head"))
        hm = HEAD_RE.search(head)
        if not hm:
            continue
        rest = m.group("body")
        pdf = re.search(r'href="([^"]+\.pdf)"', rest, re.I)
        pub = re.search(r"Publicerad:\s*(\d{4}-\d{2}-\d{2})", rest)
        paras = [strip_tags(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", rest, re.S)]
        summary = " ".join(p for p in paras
                           if p and not p.startswith(("Dokument", "Publicerad")))
        yield {
            "diarienummer": re.sub(r"\s+", "", hm.group(2)),
            "date": hm.group(1),
            "title": head,
            "area": area,
            "summary": summary,
            "published": pub.group(1) if pub else None,
            "pdf_url": urllib.parse.urljoin(url, html.unescape(pdf.group(1))) if pdf else None,
            "listing_url": url,
        }


def build_index():
    """Small metadata index (a few hundred short dicts), deduplicated by dnr."""
    index = {}
    for url in category_pages():
        slug = urllib.parse.urlparse(url).path.strip("/").replace("/", "__") or "root"
        path = os.path.join(RAW, "pages", slug + ".html")
        fetch(url, path)
        for e in parse_listing(url, read(path)):
            old = index.get(e["diarienummer"])
            if old:
                if e["area"] and e["area"] not in old["areas"]:
                    old["areas"].append(e["area"])
                continue
            e["areas"] = [e["area"]] if e["area"] else []
            index[e["diarienummer"]] = e
    return sorted(index.values(), key=lambda e: (e["date"], e["diarienummer"]),
                  reverse=True)


# ---------------------------------------------------------------- parsing --

LAW = (r"(?:studiestödslag\w*|studiestödsförordning\w*|förvaltningslag\w*|"
       r"utlänningslag\w*|skollag\w*|föräldrabalk\w*|socialförsäkringsbalk\w*|"
       r"inkomstskattelag\w*|skatteförfarandelag\w*|högskoleförordning\w*|"
       r"(?:lag|förordning)(?:en)?(?: \(\d{4}:\d+\))? om [a-zåäö-]+|"
       r"(?:lag|förordning)(?:en)? \(\d{4}:\d+\)|CSNFS \d{4}:\d+)"
       r"(?: \(\d{4}:\d+\))?")
PROV_RE = re.compile(
    r"((?:\d+\s*kap\.\s*)?\d+\s*(?:[a-z]\s)?(?:(?:,|–|-|och|eller|samt)\s*\d+\s*(?:[a-z]\s)?)*"
    r"§§?(?:\s*(?:första|andra|tredje|fjärde|femte)\s+stycket)?"
    r"(?:\s*(?:och|samt)\s*(?:\d+\s*kap\.\s*)?\d+\s*(?:[a-z]\s)?§)?)"
    r"\s+(?:i\s+)?" + LAW, re.I)

FOOTER_LINES = {
    "postadress", "besöksadress", "telefon", "e-post", "webbplats", "box 110",
    "871 23 härnösand", "backgränd 9", "010-550 86 00", "registrator@oks.se",
    "www.oks.se", "öks",
}


def clean_text(raw):
    raw = raw.replace("\x0c", "\n").replace("­", "")
    lines, seen_dnr = [], False
    for ln in raw.splitlines():
        s = ln.strip()
        if re.fullmatch(r"(Sida\s+)?\d+\s*(\(\d+\)|av\s+\d+)?", s):
            continue  # page numbers
        if s.lower() in FOOTER_LINES:
            continue  # letterhead / page footer
        if re.fullmatch(r"Dnr:?\s*[\d\s-]+", s):
            if seen_dnr:
                continue  # repeated page header
            seen_dnr = True
        # "-----------Ärendet" -> heading on its own line
        s = re.sub(r"^[-_–—]{5,}\s*", "", s)
        lines.append(s)
    text = "\n".join(lines)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # de-hyphenate line breaks
    return re.sub(r"\n{3,}", "\n\n", text).strip()


OPERATIVE_HEAD = re.compile(
    r"(?mi)^\s*(?:Överklagandenämndens\s+(?:för\s+studiestöd\s+)?(?:beslut|avgörande)|"
    r"Nämndens\s+(?:beslut|avgörande)|ÖKS:?s?\s+beslut)\s*:?\s*$")
OPERATIVE_STOP = re.compile(
    r"(?mi)^\s*(?:Ärendet|Bakgrund|Yrkanden?(?:\s+m\.?\s?m\.?)?|"
    r"Skäl(?:en)?(?:\s+för\s+beslutet)?|Överklagandenämndens\s+bedömning|"
    r"Redogörelse\s+för\s+ärendet|Tillämpliga\s+bestämmelser|Överklagandet|"
    r"Utredning(?:en)?)\s*:?\s*$")


def operative_part(text):
    """Text under the operative heading ("Overklagandenamndens beslut"),
    without the boilerplate sentence saying the decision cannot be appealed."""
    m = OPERATIVE_HEAD.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    stop = OPERATIVE_STOP.search(rest)
    part = rest[:stop.start()] if stop else rest[:1000]
    part = re.sub(r"\s+", " ", part).strip()
    part = re.sub(r"(?:Överklagandenämndens|Nämndens|Detta)\s+beslut\s+får\b.{0,200}?"
                  r"inte\s+överklagas\.?", " ", part).strip()
    sents = re.split(r"(?<=[a-zåäö0-9\)])\.\s+(?=[A-ZÅÄÖ])", part)
    keep = [x for x in sents if not re.search(r"får\b.*\binte\s+överklagas|kan\s+inte\s+överklagas", x)]
    part = ". ".join(x.rstrip(".") for x in keep).strip()
    return (part + ".")[:1500] if part else ""


RX_REMAND = re.compile(r"återförvisa|visar\s+(?:ärendet|målet)\s+åter|åter\s+till\s+CSN", re.I)
RX_NOCHANGE = re.compile(r"ändrar\s+inte|utan\s+bifall|fastställer", re.I)
RX_AVSLAG = re.compile(r"\bavslår\b|\bavslås\b", re.I)
RX_AVVIS = re.compile(r"\bavvisar\b|\bavvisas\b", re.I)
RX_BIFALL = re.compile(
    r"\bbifaller\b|\bbifalls\b|\bändrar\b|\bupphäver\b|\bupphävs\b|\bundanröjer\b|"
    r"\bbeslutar\b|\bupphävande\b|med\s+ändring\s+av|\bbeviljar\b|\bförklarar\b|\befterger\b|\bbefriar\b", re.I)
RX_PARTIAL = re.compile(r"\bdelvis\b|till\s+viss\s+del", re.I)
RX_OTHER = re.compile(r"avskriv", re.I)  # written off: not one of our labels


def classify(text):
    """Conservative outcome label, derived from the operative part only.

    bifall         = the board changes/sets aside CSN's decision or decides the
                     disputed question for the appellant (also when CSN is told
                     to "prova arendet pa nytt med denna forutsattning").
    aterforvisning = explicit remand ("aterforvisar", "visar arendet ater").
    delvis_bifall  = change + "avslar/avvisar ... i ovrigt", or "delvis".
    avslag         = "avslar overklagandet" (also with a minor part dismissed).
    avvisning      = "avvisar overklagandet" only.
    unknown        = no operative heading found, or nothing matched.
    """
    part = operative_part(text)
    if not part:
        return "unknown", ""
    remand = bool(RX_REMAND.search(part))
    nochange = bool(RX_NOCHANGE.search(part))
    avslag = bool(RX_AVSLAG.search(part)) or nochange
    avvis = bool(RX_AVVIS.search(part))
    bifall = bool(RX_BIFALL.search(re.sub(r"ändrar\s+inte", "", part, flags=re.I)))
    if RX_OTHER.search(part):
        return "unknown", part
    if remand:
        label = "delvis_bifall" if avslag and bifall and not re.search(
            r"upphäver[^.]*återförvis", part, re.I) else "aterforvisning"
        return label, part
    if bifall and (avslag or avvis or RX_PARTIAL.search(part)):
        return "delvis_bifall", part
    if bifall:
        return "bifall", part
    if avslag:
        return "avslag", part
    if avvis:
        return "avvisning", part
    return "unknown", part


def provisions(text):
    seen, out = set(), []
    for m in PROV_RE.finditer(re.sub(r"\s+", " ", text)):
        p = m.group(0).strip(" ,.;")
        key = p.lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def extract_text(pdf_path, txt_path):
    if os.path.exists(txt_path) and os.path.getsize(txt_path) > 0:
        return read(txt_path)
    exe = shutil.which("pdftotext")
    if not exe:
        return ""
    subprocess.run([exe, "-enc", "UTF-8", pdf_path, txt_path],
                   check=False, timeout=120,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return read(txt_path) if os.path.exists(txt_path) else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150,
                    help="max decisions (most recent first); PDFs, so default 150")
    ap.add_argument("--no-fetch", action="store_true",
                    help="re-parse from cache only, no network")
    args = ap.parse_args()

    for d in ("pages", "pdf", "txt"):
        os.makedirs(os.path.join(RAW, d), exist_ok=True)
    try:
        index = build_index()
    except Blocked as e:
        sys.exit("BLOCKED while listing: %s -- stopping." % e)
    print("index: %d unique decisions listed" % len(index), file=sys.stderr)

    n = 0
    tmp = OUT + ".part"
    with open(tmp, "w", encoding="utf-8") as out:
        for e in index:
            if n >= args.limit:
                break
            if not e["pdf_url"]:
                continue
            dnr = e["diarienummer"]
            pdf_path = os.path.join(RAW, "pdf", dnr + ".pdf")
            txt_path = os.path.join(RAW, "txt", dnr + ".txt")
            if not os.path.exists(pdf_path):
                if args.no_fetch:
                    continue
                try:
                    fetch(urllib.parse.quote(e["pdf_url"], safe=":/%"), pdf_path, binary=True)
                except Blocked as err:
                    print("BLOCKED: %s -- stopping." % err, file=sys.stderr)
                    break
                except (urllib.error.URLError, OSError) as err:
                    print("skip %s: %s" % (dnr, err), file=sys.stderr)
                    continue
            text = clean_text(extract_text(pdf_path, txt_path))
            outcome, outcome_raw = classify(text)
            saken = re.search(r"(?ms)^Saken\s*\n+(.*?)\n\s*\n", text)
            rec = {
                "source": "oks",
                "url": e["pdf_url"],
                "diarienummer": dnr,
                "date": e["date"],
                "title": e["title"],
                "area": e["area"],
                "provisions": provisions(text),
                "summary": e["summary"],
                "saken": re.sub(r"\s+", " ", saken.group(1)).strip() if saken else "",
                "text": text,
                "outcome_raw": outcome_raw,
                "outcome": outcome,
                "areas_all": e["areas"],
                "published": e["published"],
                "listing_url": e["listing_url"],
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            n += 1
            if n % 25 == 0:
                print("  %d done" % n, file=sys.stderr)
    os.replace(tmp, OUT)
    print("wrote %d records to %s" % (n, OUT), file=sys.stderr)


if __name__ == "__main__":
    main()
