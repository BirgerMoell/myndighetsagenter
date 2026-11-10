#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Polite collector for published supervision decisions from Skolinspektionen.

How the site serves decisions (observed 2026-11):
  * https://www.skolinspektionen.se/beslut-rapporter/sok-beslut/ is a Vue widget
    that calls a small read-only JSON API (JSON is double-encoded as a string):
        /api/siris/counties/                              municipalities
        /api/siris/counties/{kod}/schools/current         schools in municipality
        /api/siris/counties/{kod}/documents               operator-level documents
        /api/siris/schools/{kod}/documents                school-level documents
        /api/siris/companiesandorganisations/             independent operators
        /api/siris/companiesandorganisations/{kod}/documents
    A document is {"text": title, "url": PDF link, "år": year[, "gransknomr"]}.
    There is no free-text search, no paging and no decision-type filter.
  * /beslut-rapporter/sok-beslut/senaste-beslut/ is plain HTML listing the
    decisions published during the last 20 days (title, date, PDF link).
  * The decision itself is a PDF at siris.skolverket.se/siris/ris.openfile?docID=N.

Only plain GET requests, >= 1 s apart, generic User-Agent, everything cached on
disk, one document handled at a time, resumable (re-running replays from cache).

Usage:
    python3 collect_skolinspektionen.py --limit 200
    python3 collect_skolinspektionen.py --parse-only
"""
from __future__ import print_function

import argparse
import hashlib
import html
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "https://www.skolinspektionen.se"
SIRIS_PDF = "https://siris.skolverket.se/siris/ris.openfile?docID=%s"
USER_AGENT = "Mozilla/5.0 (compatible; research-collector/1.0)"
MIN_DELAY = 1.2
MAX_TOTAL_BYTES = int(os.environ.get("SI_MAX_MB", "280")) * 1024 * 1024

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, "data", "raw", "skolinspektionen")
OUT = os.path.join(ROOT, "data", "skolinspektionen_decisions.jsonl")
MANIFEST = os.path.join(RAW, "manifest.jsonl")

YEARS = tuple(os.environ.get("SI_YEARS", "2024,2025,2026").split(","))


class Blocked(Exception):
    pass


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
_last_request = [0.0]
_bytes_downloaded = [0]


def dir_size(path):
    total = 0
    for dp, _dn, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return total


def http_get(url, accept="*/*"):
    """GET with rate limit. Returns bytes, or None on a skippable error."""
    for attempt in (1, 2):
        wait = MIN_DELAY - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        try:
            _last_request[0] = time.time()
            resp = urllib.request.urlopen(req, timeout=60)
            data = resp.read()
            _last_request[0] = time.time()
            _bytes_downloaded[0] += len(data)
            head = data[:3000].lower()
            if b"captcha" in head or b"access denied" in head:
                raise Blocked("block/CAPTCHA page at %s" % url)
            return data
        except urllib.error.HTTPError as e:
            _last_request[0] = time.time()
            if e.code in (401, 403, 429):
                raise Blocked("HTTP %d at %s" % (e.code, url))
            if e.code >= 500 and attempt == 1:
                time.sleep(10)
                continue
            sys.stderr.write("skip %s: HTTP %d\n" % (url, e.code))
            return None
        except (urllib.error.URLError, OSError) as e:
            _last_request[0] = time.time()
            if attempt == 1:
                time.sleep(10)
                continue
            sys.stderr.write("skip %s: %s\n" % (url, e))
            return None
    return None


def cached_get(url, path, accept="*/*"):
    """Return path of cached file (downloading if needed) or None."""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    data = http_get(url, accept)
    if data is None:
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


def api_json(endpoint, name):
    path = cached_get(BASE + endpoint, os.path.join(RAW, "api", name),
                      "application/json")
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        try:
            d = json.load(f)
            if isinstance(d, str):  # API double-encodes
                d = json.loads(d)
            return d
        except ValueError:
            return None


# --------------------------------------------------------------------------
# Listing / candidates
# --------------------------------------------------------------------------
FORM_WORDS = (
    "Anpassad grundskola|Anpassad gymnasieskola|Anpassade grundskolan|"
    "Anpassade gymnasieskolan|Förskoleklass|Grundskolan|Grundskola|"
    "Gymnasieskolan|Gymnasieskola|Fritidshem|Vuxenutbildning|Förskola|"
    "Specialskola|Sameskola|Grundsärskola|Gymnasiesärskola|Flera")
TYPE_RE = re.compile(
    r"^(?:(?P<pre>Beslut|Uppföljningsbeslut)\s+)?(?P<kind>(?:Planerad|Riktad|Tematisk|Regelbunden|Regelb\.)"
    r"\s+tillsyn)[,\s]*"
    r"(?P<sub>Uppföljning\s+(?:skol|huvudmanna)beslut|Uppföljningsbeslut|"
    r"Uppföljning\s+beslut|Skolbeslut|Huvudmannabeslut)?[,\s]*"
    r"(?P<form>" + FORM_WORDS + r")?", re.I)


def clean_title(t):
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*\(pdf,[^)]*\)\s*$", "", t)
    return t


def is_candidate(title, year):
    if year not in YEARS:
        return False
    low = title.lower()
    if "tillsyn" not in low:
        return False
    if "kvalitetsgranskning" in low or "enkät" in low:
        return False
    return True


def title_info(title):
    m = TYPE_RE.match(title)
    if not m:
        return None, None
    kind = re.sub(r"\s+", " ", m.group("kind"))
    kind = kind[0].upper() + kind[1:]
    sub = m.group("sub")
    if not sub and (m.group("pre") or "").lower() == "uppföljningsbeslut":
        sub = "Uppföljningsbeslut"
    dtype = kind + (" " + re.sub(r"\s+", " ", sub) if sub else "")
    return dtype, m.group("form")


def doc_id(url):
    m = re.search(r"docID=(\d+)", url)
    return m.group(1) if m else None


def latest_candidates():
    path = cached_get(BASE + "/beslut-rapporter/sok-beslut/senaste-beslut/",
                      os.path.join(RAW, "senaste-beslut.html"), "text/html")
    if not path:
        return
    with open(path, encoding="utf-8") as f:
        h = f.read()
    for m in re.finditer(
            r"href=[\"']?(http[^\s\"'>]+docID=(\d+))[\"']?>\s*(.*?)\s*</a>"
            r".*?<p>(\d{4}-\d\d-\d\d)</p>", h, re.S):
        title = clean_title(m.group(3))
        title = re.sub(r"(, \d{4}), \d{4}$", r"\1", title)
        yield {"doc_id": m.group(2), "title": title, "year": m.group(4)[:4],
               "listing_date": m.group(4), "via": "senaste-beslut"}


def api_candidates(rng, schools_per_municipality):
    counties = api_json("/api/siris/counties/", "counties.json") or []
    companies = api_json("/api/siris/companiesandorganisations/",
                         "companies.json") or []
    if companies and isinstance(companies[0], dict) and "aktuella" in companies[0]:
        companies = companies[0]["aktuella"]
    counties = list(counties)
    companies = list(companies)
    rng.shuffle(counties)
    rng.shuffle(companies)
    ci = 0
    for county in counties:
        kod, namn = county["kod"], county["namn"]
        docs = api_json("/api/siris/counties/%s/documents" % kod,
                        "county_%s_documents.json" % kod) or {}
        for d in docs.get("svar", []):
            yield {"doc_id": doc_id(d["url"]), "title": clean_title(d["text"]),
                   "year": d.get("år", ""), "municipality_hint": namn,
                   "via": "county:%s" % kod}
        sch = api_json("/api/siris/counties/%s/schools/current" % kod,
                       "county_%s_schools.json" % kod) or []
        if sch and isinstance(sch[0], dict) and "aktuella" in sch[0]:
            sch = sch[0]["aktuella"]
        seen, schools = set(), []
        for s in sch:
            if s["kod"] not in seen:
                seen.add(s["kod"])
                schools.append(s)
        rng.shuffle(schools)
        for s in schools[:schools_per_municipality]:
            docs = api_json("/api/siris/schools/%s/documents" % s["kod"],
                            "school_%s_documents.json" % s["kod"]) or {}
            for d in docs.get("svar", []):
                yield {"doc_id": doc_id(d["url"]),
                       "title": clean_title(d["text"]),
                       "year": d.get("år", ""), "municipality_hint": namn,
                       "school_hint": s["namn"], "via": "school:%s" % s["kod"]}
        # a couple of independent operators after each municipality
        for comp in companies[ci:ci + 2]:
            docs = api_json(
                "/api/siris/companiesandorganisations/%s/documents" % comp["kod"],
                "company_%s_documents.json" % comp["kod"]) or {}
            for d in docs.get("svar", []):
                yield {"doc_id": doc_id(d["url"]),
                       "title": clean_title(d["text"]),
                       "year": d.get("år", ""), "operator_hint": comp["namn"],
                       "via": "company:%s" % comp["kod"]}
        ci += 2


# --------------------------------------------------------------------------
# Manifest (resumable state)
# --------------------------------------------------------------------------
def load_manifest():
    items = []
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    items.append(json.loads(line))
    return items


def save_manifest(items):
    tmp = MANIFEST + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    os.replace(tmp, MANIFEST)


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(limit, latest_cap, schools_per_municipality, seed):
    os.makedirs(os.path.join(RAW, "pdf"), exist_ok=True)
    items = load_manifest()
    by_id = dict((it["doc_id"], it) for it in items)
    by_md5 = dict((it["md5"], it) for it in items)
    type_count, year_count = {}, {}
    for it in items:
        type_count[it["decision_type"]] = type_count.get(it["decision_type"], 0) + 1
        year_count[it["year"]] = year_count.get(it["year"], 0) + 1
    type_cap = max(5, int(limit * 0.25))
    year_cap = max(5, int(limit * 0.45))
    rng = random.Random(seed)
    n_latest = sum(1 for it in items if it.get("via") == "senaste-beslut")

    def consider(c, is_latest):
        if not c.get("doc_id") or not is_candidate(c["title"], c["year"]):
            return
        if c["doc_id"] in by_id:
            return
        dtype, form = title_info(c["title"])
        if not dtype:
            sys.stderr.write("unrecognised title: %s\n" % c["title"])
            dtype = "okänd"
        if type_count.get(dtype, 0) >= type_cap:
            return
        if year_count.get(c["year"], 0) >= year_cap:
            return
        if dir_size(RAW) > MAX_TOTAL_BYTES:
            raise Blocked("size budget reached")
        pdf = cached_get(SIRIS_PDF % c["doc_id"],
                         os.path.join(RAW, "pdf", c["doc_id"] + ".pdf"),
                         "application/pdf")
        if not pdf:
            return
        with open(pdf, "rb") as f:
            if f.read(5) != b"%PDF-":
                sys.stderr.write("not a PDF: %s\n" % c["doc_id"])
                return
        digest = md5_file(pdf)
        if digest in by_md5:  # same decision listed under another school form
            first = by_md5[digest]
            if form and form not in first["school_forms"]:
                first["school_forms"].append(form)
            first.setdefault("alias_doc_ids", [])
            if c["doc_id"] not in first["alias_doc_ids"]:
                first["alias_doc_ids"].append(c["doc_id"])
            by_id[c["doc_id"]] = first
            save_manifest(items)
            return
        c["decision_type"] = dtype
        c["school_forms"] = [form] if form else []
        c["md5"] = digest
        c["url"] = SIRIS_PDF % c["doc_id"]
        items.append(c)
        by_id[c["doc_id"]] = c
        by_md5[digest] = c
        type_count[dtype] = type_count.get(dtype, 0) + 1
        year_count[c["year"]] = year_count.get(c["year"], 0) + 1
        save_manifest(items)
        print("[%d/%d] %s | %s" % (len(items), limit, c["doc_id"], c["title"]))
        sys.stdout.flush()

    for c in latest_candidates():
        if len(items) >= limit or n_latest >= latest_cap:
            break
        before = len(items)
        consider(c, True)
        n_latest += len(items) - before
    if len(items) < limit:
        for c in api_candidates(rng, schools_per_municipality):
            if len(items) >= limit:
                break
            consider(c, False)
    return items


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def pdf_to_text(pdf_path):
    txt_path = os.path.join(RAW, "txt",
                            os.path.basename(pdf_path)[:-4] + ".txt")
    if not os.path.exists(txt_path):
        os.makedirs(os.path.dirname(txt_path), exist_ok=True)
        subprocess.run(["pdftotext", "-enc", "UTF-8", pdf_path, txt_path],
                       check=False, timeout=120)
    if not os.path.exists(txt_path):
        return ""
    with open(txt_path, encoding="utf-8", errors="replace") as f:
        return f.read()


FURNITURE = re.compile(
    r"^(Skolinspektionen, Box .*|www\.skolinspektionen\.se|SKOLINSPEKTIONEN|"
    r"Sida \d+ ?\(\d+\)|\d+ ?\(\d+\))$")


def clean_text(raw):
    lines = []
    for ln in raw.replace("\x0c", "\n").split("\n"):
        ln = ln.rstrip()
        if FURNITURE.match(ln.strip()):
            continue
        lines.append(ln)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


SECTION_END = re.compile(
    r"^(Om ärendet|Redovisning av åtgärder|Motivering av beslutet|Bakgrund|"
    r"Ärendets handläggning|Skälen för beslutet|Skäl för beslutet|"
    r"Uppföljning|Hur man överklagar|Sammanfattning|Ärendet|Bedömning|"
    r"Inledning|Skolinspektionens bedömning)\s*$")
OUTCOME_HEAD = re.compile(
    r"^(Föreläggande( med vite| vid vite)?|Anmärkning|"
    r"Avstående från ingripande|Återkallelse.*)$")


def split_sections(text):
    """Return (arendet, beslut_section, body_after)."""
    lines = text.split("\n")
    a_idx = None
    for i, ln in enumerate(lines[:120]):
        if ln.strip() in ("Ärendet", "Ärende"):
            a_idx = i
            break
    arendet = ""
    a_end = None
    if a_idx is not None:
        a_end = a_idx + 1
        while a_end < len(lines) and lines[a_end].strip() \
                and not lines[a_end].startswith("___") \
                and lines[a_end].strip() not in ("Beslut", "Beslut:") \
                and not OUTCOME_HEAD.match(lines[a_end].strip()):
            a_end += 1
        arendet = re.sub(r"\s+", " ", " ".join(
            l.strip() for l in lines[a_idx + 1:a_end])).strip()
    b_idx = None
    # (a) older layout: "Skolinspektionens beslut" heading (may precede Ärendet)
    for i, ln in enumerate(lines[:120]):
        if ln.strip() == "Skolinspektionens beslut":
            b_idx = i + 1
            break
    # (b) "Beslut" heading after Ärendet, or (c) an outcome heading directly
    if b_idx is None and a_end is not None:
        for i in range(a_end, min(len(lines), a_end + 12)):
            st = lines[i].strip()
            if st in ("Beslut", "Beslut:"):
                b_idx = i + 1
                break
            if OUTCOME_HEAD.match(st):
                b_idx = i
                break
    # (c2) "Beslut" heading further down (e.g. after "Beslutet i korthet");
    # must be followed by an outcome heading or a sentence, since older
    # layouts repeat "Beslut" as a page header
    if b_idx is None and a_end is not None:
        for i in range(a_end + 12, min(len(lines) - 1, a_end + 200)):
            if lines[i].strip() in ("Beslut", "Beslut:"):
                j = i + 1
                while j < len(lines) - 1 and not lines[j].strip():
                    j += 1
                nxt = lines[j].strip()
                if OUTCOME_HEAD.match(nxt) or nxt.startswith("Skolinspektionen "):
                    b_idx = i + 1
                    break
    # (d) short decisions without any heading: everything after Ärendet
    if b_idx is None and a_end is not None and len(text) < 3000:
        return arendet, "\n".join(lines[a_end:]).strip(), ""
    if b_idx is None:
        return arendet, "", text
    e_idx = len(lines)
    for i in range(b_idx + 1, len(lines)):
        if SECTION_END.match(lines[i].strip()):
            e_idx = i
            break
    return (arendet, "\n".join(lines[b_idx:e_idx]).strip(),
            "\n".join(lines[e_idx:]))


SEVERITY = ["ingen_brist", "uppfoljning_avslutad", "avstående_från_ingripande",
            "anmarkning", "foreläggande", "foreläggande_med_vite", "aterkallelse"]

OUTCOME_HINT = re.compile(
    r"förelägg|avslutar|avstår|anmärkning|återkalla|vite|förbjud|avskriv|"
    r"verksamhetsförbud|statliga åtgärder", re.I)


def sentences(section):
    flat = re.sub(r"\s+", " ", section)
    # do not split on "kap." / "p." / "t.ex." / dates
    parts = re.split(r"(?<=[a-zåäö\)\d]{2}[.!?])\s+(?=[A-ZÅÄÖ])", flat)
    return [p.strip() for p in parts if p.strip()]


def derive_outcome(beslut, body, is_followup):
    found = []
    sents = sentences(beslut)
    raw = []
    prev_low = ""
    for s in sents:
        low = s.lower()
        if OUTCOME_HINT.search(low):
            raw.append(s)
        labels = []
        if re.search(r"\båterkallar\b", low):
            labels.append("aterkallelse")
        if re.search(r"\bförelägger\b", low):
            if re.search(r"\bvid vite\b", low):
                labels.append("foreläggande_med_vite")
            else:
                labels.append("foreläggande")
        if re.search(r"\btilldelar\b.*\banmärkning\b", low):
            labels.append("anmarkning")
        if re.search(r"\bavstår\b.*\bfrån att ingripa\b", low) or \
                re.search(r"\bavstår från ingripande\b", low):
            labels.append("avstående_från_ingripande")
        if not labels and not is_followup and re.search(
                r"\bavskriver\b.*\bärendet\b|\bärendet avskrivs\b", low):
            labels.append("ingen_brist")  # closed without action
        elif not labels and re.search(
                r"\bavslutar\b.*\b(ärendet|tillsynen|uppföljningen)\b", low):
            tied_to_waiver = ("därmed" in low and "avstår" in prev_low)
            if not tied_to_waiver:
                if "utan konstaterade brister" in low:
                    labels.append("ingen_brist")
                elif is_followup:
                    if re.search(r"avhjälpt|följt föreläggandet|"
                                 r"vidtagit (?:tillräckliga |de )?åtgärder",
                                 re.sub(r"\s+", " ",
                                        (beslut + " " + body).lower())):
                        labels.append("uppfoljning_avslutad")
                else:
                    labels.append("ingen_brist")
        for l in labels:
            if l not in found:
                found.append(l)
        prev_low = low
    outcome_raw = " ".join(raw)[:2000]
    if not found:
        return "unknown", [], outcome_raw
    found.sort(key=lambda l: -SEVERITY.index(l))
    return found[0], found, outcome_raw


PROV_RE = re.compile(
    r"(\d{1,2}\s*kap\.?\s*\d{1,3}(?:\s?[a-z]\b)?"
    r"(?:\s*(?:,|och|samt|–|-)\s*\d{1,3}(?:\s?[a-z]\b)?)*\s*§§?"
    r"(?:\s+(?:första|andra|tredje|fjärde|femte|sjätte)"
    r"(?:\s+(?:och|–|-)\s+(?:andra|tredje|fjärde|femte|sjätte))?\s+stycket)?"
    r"(?:\s+(?:p\.?|punkten)\s?\d)?\s+"
    r"(skollagen|skolförordningen|gymnasieförordningen|"
    r"förordningen om vuxenutbildning|diskrimineringslagen|"
    r"förvaltningslagen|socialtjänstlagen|offentlighets- och sekretesslagen|"
    r"kommunallagen|arbetsmiljölagen))", re.I)

AREA_STOP = set(x.lower() for x in (
    "Motivering av beslutet", "Föreläggande", "Anmärkning",
    "Avstående från ingripande", "Skolinspektionens bedömning",
    "Rättslig reglering", "Om ärendet", "Beslut",
    "Områden utan konstaterade brister", "Föreläggande med vite",
    "Föreläggande vid vite"))


def find_areas(text, beslut, after=""):
    lines = [l.strip() for l in text.split("\n")]
    areas = []
    for i, ln in enumerate(lines):
        if not ln.startswith("Omständigheter som ligger till grund"):
            continue
        j = i - 1
        while j >= 0 and not lines[j]:
            j -= 1
        if j < 0:
            continue
        head = lines[j]
        k = 0
        while head and not head[0].isupper() and j > 0 and lines[j - 1] and k < 2:
            j -= 1
            k += 1
            head = lines[j] + " " + head
        head = re.sub(r"\s+", " ", head).strip()
        if (not head or len(head) > 160 or head.endswith(".")
                or head.lower() in AREA_STOP or not head[0].isupper()):
            continue
        if head not in areas:
            areas.append(head)
    if not areas:  # fallback: areas enumerated in the decision section
        flat = re.sub(r"\s+", " ", beslut + " " + after[:3000])
        for m in re.finditer(
                r"(?:brist(?:er|en)? (?:rörande|avseende|gällande)|"
                r"i de delar det avser|"
                r"bestämmelser(?:na)? (?:om|gällande|avseende)|"
                r"[Tt]illsynen har omfattat(?: prövning av)?)\s+"
                r"(?!bestämmelser)([^.]{3,300})\.", flat):
            for part in re.split(r",\s*|\s+samt\s+", m.group(1)):
                part = part.strip()
                if 2 < len(part) < 120:
                    part = part[0].upper() + part[1:]
                    if part not in areas:
                        areas.append(part)
    return areas


FORM_IN_TEXT = re.compile(
    r"^(?:(?:och|samt|,)\s+)?(förskoleklass(?:en)?|grundskolan?|"
    r"gymnasieskolan?|anpassade? grundskolan?|anpassade? gymnasieskolan?|"
    r"fritidshem(?:met)?|förskolan?|kommunal vuxenutbildning|"
    r"vuxenutbildning(?:en)?|komvux|specialskolan?|sameskolan?|"
    r"skolenheten|utbildningen vid|vid)\b[,\s]*", re.I)

_municipalities = []


def municipality_names():
    if not _municipalities:
        p = os.path.join(RAW, "api", "counties.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, str):
                    d = json.loads(d)
            _municipalities.extend(c["namn"] for c in d)
    return _municipalities


def norm_municipality(name):
    name = name.strip()
    names = municipality_names()
    for n in names:
        if n.lower() == name.lower():
            return n
    for n in names:
        if (n + "s").lower() == name.lower():
            return n
    return name


def parse_arendet(arendet):
    """-> (school, municipality, forms)"""
    school = municipality = None
    forms = []
    m = re.search(r"\bi\s+([A-ZÅÄÖ][\wåäöÅÄÖé\-]*(?:\s[A-ZÅÄÖ][\wåäöÅÄÖ\-]*)?)\s+"
                  r"(?:kommun|stad)\b", arendet)
    if m:
        municipality = norm_municipality(m.group(1))
    m2 = re.search(r"Tillsyn (?:av|i|vid)\s+(.+?)(?:\s+i\s+[A-ZÅÄÖ][\wåäöÅÄÖé\- ]*?"
                   r"\s(?:kommun|stad)\b|[;.]|$)", arendet)
    if m2:
        what = m2.group(1).strip()
        while True:
            fm = FORM_IN_TEXT.match(what)
            if not fm:
                break
            f = fm.group(1).lower()
            if f not in ("skolenheten", "utbildningen vid", "vid"):
                forms.append(f)
            what = what[fm.end():].strip()
        if what and what[0].isupper() or (what and what[0].isdigit()):
            school = what.strip(" ,")
    return school, municipality, forms


def operator_type(op):
    if not op:
        return "unknown"
    low = op.lower()
    if re.search(r"\bkommun(en)?\b|\bstad\b|kommunalförbund|"
                 r"utbildningsförbund|gymnasieförbund", low):
        return "kommunal"
    if re.search(r"\bab\b|aktiebolag|stiftelse|förening|\bek\.?\b|kooperativ|"
                 r"\bhb\b|handelsbolag|\bkb\b|\bubf\b|församling|\(publ\)", low):
        return "enskild"
    return "unknown"


DASH = "[-–‐‑−]"


def parse_header(text):
    cut = re.search(r"^Ärendet?\s*$", text[:2500], re.M)
    head = text[:cut.start()] if cut else text[:800]
    dnr = date = operator = None
    m = re.search(r"(?:Dnr\.?\s*:?\s*(?:SI:?\s*)?|\bSI:?\s*)(\d{4}):\s?(\d{1,6})",
                  head, re.I)
    if m:
        dnr = "SI %s:%s" % m.groups()  # normalised; published forms vary
    m = re.search(r"\b(20\d{2})%s(\d{2})%s(\d{2})\b" % (DASH, DASH), head)
    if m:
        date = "%s-%s-%s" % m.groups()
    lines = [l.strip() for l in head.split("\n")]
    start = lines.index("Huvudman") + 1 if "Huvudman" in lines else 0
    skip = re.compile(
        r"^(Beslut|Huvudman|Kommun|Datum\b.*|Dnr\b.*|SI:? ?\d{4}.*|Sida\b.*|"
        r"\d+ ?\(\d+\)|\d{4}%s\d{2}%s\d{2}|Organisationsnummer.*|"
        r"Org\.? ?nr.*|Rektor\b.*|Skolinspektionen|Box \d+.*|\d{3} ?\d{2} .*)$"
        % (DASH, DASH), re.I)
    for i in range(start, len(lines)):
        cand = lines[i]
        if not cand or "@" in cand or skip.match(cand):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt and "@" not in nxt and not skip.match(nxt) and (
                nxt[0].islower() or re.match(r"(Sverige AB|AB|Förening)\b", nxt)):
            cand = cand + " " + nxt
        cand = re.sub(r"[,\s]*(\(?\b(org\b|med organisationsnummer|"
                      r"organisationsnummer).*|\s\d{6}%s?\d{4})$" % DASH,
                      "", cand, flags=re.I)
        operator = cand.strip(" ,")
        break
    return dnr, date, operator


def tidy_operator(op):
    if op and op.isupper():
        op = " ".join(w.capitalize() if w not in ("AB",) else w
                      for w in op.split())
        op = re.sub(r"\bKommun\b", "kommun", op)
    return op


def parse_record(item):
    pdf = os.path.join(RAW, "pdf", item["doc_id"] + ".pdf")
    text = clean_text(pdf_to_text(pdf))
    dnr, date, operator = parse_header(text)
    arendet, beslut, body = split_sections(text)
    school, municipality, forms_txt = parse_arendet(arendet)
    title = item["title"]
    is_followup = ("uppföljning" in title.lower()
                   or "uppföljning" in arendet.lower())
    outcome, outcomes_all, outcome_raw = derive_outcome(beslut, body, is_followup)
    operator = tidy_operator(operator) or item.get("operator_hint")
    forms = [f for f in item.get("school_forms", []) if f and f != "Flera"]
    if not forms:
        forms = [f.capitalize() for f in forms_txt]
    provisions = []
    for m in PROV_RE.finditer(re.sub(r"\s+", " ", text)):
        p = re.sub(r"\s+", " ", m.group(1)).strip()
        p = re.sub(r"kap\.?\s*", "kap. ", p)
        if p not in provisions:
            provisions.append(p)
    return {
        "source": "skolinspektionen",
        "url": item["url"],
        "diarienummer": dnr,
        "date": date,
        "title": title,
        "decision_type": item["decision_type"],
        "school": item.get("school_hint") or school,
        "operator": operator,
        "operator_type": operator_type(operator),
        "school_form": ", ".join(forms) if forms else None,
        "municipality": item.get("municipality_hint") or municipality,
        "areas_assessed": find_areas(text, beslut, body),
        "provisions": provisions,
        "text": text,
        "outcome_raw": outcome_raw,
        "outcome": outcome,
        "outcomes_all": outcomes_all,
        "case_summary": arendet or None,
        "is_followup": is_followup,
        "doc_id": item["doc_id"],
        "listing_year": item["year"],
    }


def parse_all(items):
    tmp = OUT + ".part"
    n = 0
    with open(tmp, "w", encoding="utf-8") as out:
        for it in items:  # one document at a time
            rec = parse_record(it)
            if not rec["text"]:
                sys.stderr.write("no text extracted: %s\n" % it["doc_id"])
                continue
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    os.replace(tmp, OUT)
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=200,
                    help="number of decisions to collect")
    ap.add_argument("--latest-cap", type=int, default=None,
                    help="max decisions taken from the 'Senaste beslut' page "
                         "(default: 30%% of --limit)")
    ap.add_argument("--schools-per-municipality", type=int, default=12)
    ap.add_argument("--seed", type=int, default=20261110)
    ap.add_argument("--parse-only", action="store_true",
                    help="no network; re-parse cached documents")
    args = ap.parse_args()
    os.makedirs(RAW, exist_ok=True)
    if args.parse_only:
        items = load_manifest()
    else:
        cap = args.latest_cap if args.latest_cap is not None \
            else int(args.limit * 0.3)
        try:
            items = collect(args.limit, cap, args.schools_per_municipality,
                            args.seed)
        except Blocked as e:
            sys.stderr.write("STOPPED: %s\n" % e)
            items = load_manifest()
    n = parse_all(items[:args.limit] if not args.parse_only else items)
    print("wrote %d records to %s (downloaded %.1f MB this run, raw dir %.1f MB)"
          % (n, OUT, _bytes_downloaded[0] / 1e6, dir_size(RAW) / 1e6))


if __name__ == "__main__":
    main()
