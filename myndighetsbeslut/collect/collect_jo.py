#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Polite collector for published JO decisions (Riksdagens ombudsmaen, www.jo.se).

How the site serves decisions
-----------------------------
* The public search page https://www.jo.se/jo-beslut/sokresultat/ is a small
  JS app that POSTs a read-only search request to
  /wp/wp-admin/admin-ajax.php (action=get_jo_search_result) with a public
  nonce embedded in the page. robots.txt explicitly allows admin-ajax.php.
  The response holds, per decision: title, permalink, summary (post_content),
  resolve_date, diary_number, decision maker, taxonomy tags (authority
  category, matter of fact, legal regulation), pdf_url and a flattened
  pdf_text.
* The full decision is a PDF under /app/uploads/resolve_pdfs/. We fetch it,
  run `pdftotext` (keeps line structure, needed for section headings), keep
  the .txt and delete the PDF.

Politeness / safety
-------------------
* One request at a time, >= 1 s between requests, fixed anonymous User-Agent.
* Everything is cached under data/raw/jo/ and the run is resumable.
* Hard byte budget (default 1 GB) persisted across runs.
* Stops (exit code 2) on 403/429/503 or anything looking like a block.

Usage
-----
    python3 collect_jo.py --limit 1500          # collect + parse
    python3 collect_jo.py --parse-only          # rebuild JSONL from cache
    python3 collect_jo.py --stats               # sanity statistics
"""
from __future__ import print_function

import argparse
import collections
import html
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (compatible; research-collector/1.0)"
BASE = "https://www.jo.se"
SEARCH_PAGE = BASE + "/jo-beslut/sokresultat/"
AJAX_URL = BASE + "/wp/wp-admin/admin-ajax.php"
MIN_DELAY = 1.2
PAGE_SIZE = 50

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw", "jo")
HITS_DIR = os.path.join(RAW, "hits")
TXT_DIR = os.path.join(RAW, "txt")
PDF_DIR = os.path.join(RAW, "pdf")
ORDER_FILE = os.path.join(RAW, "order.jsonl")
STATE_FILE = os.path.join(RAW, "state.json")
OUT_FILE = os.path.join(ROOT, "data", "jo_decisions.jsonl")


class Blocked(Exception):
    pass


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Client(object):
    def __init__(self, budget_bytes):
        self.last = 0.0
        self.budget = budget_bytes
        self.state = {"bytes": 0}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                self.state.update(json.load(f))
        self.nonce = None

    def _save(self):
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, STATE_FILE)

    def request(self, url, data=None, dest=None):
        """GET (or the page's own read-only POST). Returns bytes, or writes
        to dest in chunks and returns the byte count."""
        if self.state["bytes"] >= self.budget:
            raise Blocked("download budget exhausted (%d bytes)" % self.state["bytes"])
        wait = MIN_DELAY - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=90)
        except urllib.error.HTTPError as e:
            self.last = time.time()
            if e.code in (401, 403, 429, 503):
                raise Blocked("HTTP %d for %s" % (e.code, url))
            raise
        try:
            if dest is None:
                out = resp.read()
                n = len(out)
            else:
                n = 0
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        n += len(chunk)
                os.replace(tmp, dest)
                out = n
        finally:
            resp.close()
            self.last = time.time()
        self.state["bytes"] += n
        self._save()
        return out

    def get_nonce(self):
        page = self.request(SEARCH_PAGE).decode("utf-8", "replace")
        if re.search(r"captcha|cf-challenge", page, re.I):
            raise Blocked("search page looks like a challenge page")
        m = re.search(r'"ajaxNonce":"([0-9a-f]+)"', page)
        if not m:
            raise Blocked("no ajax nonce found on search page")
        self.nonce = m.group(1)

    def search(self, page):
        if not self.nonce:
            self.get_nonce()
        data = [
            ("action", "get_jo_search_result"), ("_ajax_nonce", self.nonce),
            ("global_search", "0"), ("sort_order", "desc"),
            ("search_string", ""), ("search_case_number", ""),
            ("date_from", ""), ("date_to", ""),
            ("hits_per_page", str(PAGE_SIZE)), ("page", str(page)),
            ("combine_type", json.dumps({"taxonomies": "and", "authorities": "and",
                                         "matter_of_facts": "and",
                                         "legal_regulations": "and"})),
            ("authorities_filter", ""), ("matter_of_facts_filter", ""),
            ("legal_regulations_filter", ""), ("post_types", ""),
            ("language", "sv"), ("advanced_search", "0"),
        ]
        raw = self.request(AJAX_URL, data=data)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise Blocked("search endpoint returned non-JSON: %r" % raw[:200])


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------
def load_order():
    ids = []
    if os.path.exists(ORDER_FILE):
        with open(ORDER_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    ids.append(json.loads(line))
    return ids


def collect(limit, budget):
    for d in (HITS_DIR, TXT_DIR, PDF_DIR):
        os.makedirs(d, exist_ok=True)
    client = Client(budget)
    order = load_order()                      # [{"id":..., "date":...}]
    seen = set(o["id"] for o in order)

    # ---- stage 1: listing (newest first) --------------------------------
    total = None
    if len(order) < limit:
        page = len(order) // PAGE_SIZE + 1
        while len(order) < limit:
            res = client.search(page)
            total = res.get("total_hits")
            hits = res.get("search_hits") or []
            if not hits:
                break
            with open(ORDER_FILE, "a") as of:
                for h in hits:
                    hid = h.get("id")
                    if hid in seen:
                        continue
                    h.pop("_formatted", None)
                    with open(os.path.join(HITS_DIR, "%s.json" % hid), "w") as f:
                        json.dump(h, f, ensure_ascii=False)
                    seen.add(hid)
                    entry = {"id": hid, "date": h.get("resolve_date")}
                    order.append(entry)
                    of.write(json.dumps(entry) + "\n")
            print("listing page %d: %d known (site total %s)" % (page, len(order), total))
            sys.stdout.flush()
            del res, hits
            if page >= 400:
                break
            page += 1
        if total is not None:
            client.state["site_total"] = total
            client._save()

    # ---- stage 2: full text, one document at a time ---------------------
    done = 0
    for entry in order[:limit]:
        hid = entry["id"]
        txt = os.path.join(TXT_DIR, "%s.txt" % hid)
        if os.path.exists(txt):
            done += 1
            continue
        with open(os.path.join(HITS_DIR, "%s.json" % hid)) as f:
            hit = json.load(f)
        url = hit.get("pdf_url") or ""
        ok = False
        if url.startswith(BASE + "/") and url.lower().endswith(".pdf"):
            pdf = os.path.join(PDF_DIR, "%s.pdf" % hid)
            try:
                client.request(url, dest=pdf)
                r = subprocess.run(["pdftotext", "-enc", "UTF-8", pdf, txt + ".part"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=120)
                if r.returncode == 0 and os.path.getsize(txt + ".part") > 200:
                    os.replace(txt + ".part", txt)
                    ok = True
            except Blocked:
                raise
            except Exception as e:           # 404, timeout, broken pdf
                print("  pdf failed for %s: %s" % (hid, e))
            finally:
                for p in (pdf, pdf + ".part", txt + ".part"):
                    if os.path.exists(p):
                        os.remove(p)
        if not ok:
            # fall back to the flattened text delivered by the search request
            with open(txt, "w") as f:
                f.write("[FLAT]\n" + (hit.get("pdf_text") or ""))
        done += 1
        if done % 25 == 0:
            print("texts: %d/%d  (%.1f MB downloaded so far)" % (
                done, min(limit, len(order)), client.state["bytes"] / 1e6))
            sys.stdout.flush()
    print("collected %d texts, %.1f MB downloaded in total" % (done, client.state["bytes"] / 1e6))


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
BOILER = re.compile(
    r"^(\[P\]|BESLUT|Datum|Dnr|Sid|Riksdagens ombudsmän|Riksdagens ombudsmän – JO|"
    r"Postadress:.*|Besöksadress:.*|Besök:.*|Telefon:.*|Texttelefon:.*|E-post:.*|Fax:.*|"
    r"www\.jo\.se|Box 16327|103 26 Stockholm|Västra Trädgårdsgatan 4 ?A?|"
    r"Dnr\s+[\d\-–, och]+|Sid\s+\d+\s*\(\d+\)|\d+\s*\(\d+\)|\d{4}-\d{2}-\d{2}|"
    r"\d{1,6}\s?-\s?\d{4}|(Chefs)?[Jj]ustitieombudsmannen|Stf justitieombudsmannen|"
    r"Ställföreträdande (justitie)?ombudsmannen|Tillförordnade justitieombudsmannen)$")

SENT_END = (".", "!", "?", ":", ")", "”", '"', "»", "…")
FACT_HEAD = re.compile(
    r"^(anmäl|initiativ|bakgrund|utredning|inledning|ärendet|klagomål|"
    r"redogörelse|yttrande|remiss|iakttagelser|händelse|uppgifter)", re.I)
ASSESS_HEAD = re.compile(
    r"(^|\b)(bedömning|bedömningar|överväganden|ställningstagande|"
    r"slutsats|slutsatser|kritik|avslutande synpunkter|sammanfattande synpunkter)", re.I)
LEGAL_HEAD = re.compile(r"^(rättslig|rättsliga|gällande rätt|regler|den rättsliga|bestämmelser|"
                        r"tillämpliga bestämmelser|författnings)", re.I)


def clean_lines(raw, maker):
    """pdftotext output -> list of content lines without page furniture."""
    maker_parts = set()
    if maker:
        maker_parts.add(maker.strip())
        w = maker.split()
        if len(w) >= 3:
            maker_parts.add(" ".join(w[1:]))
            maker_parts.add(w[0])
    out = []
    for ln in raw.replace("\f", "\n").split("\n"):
        s = ln.strip()
        if not s:
            out.append("")
            continue
        if BOILER.match(s) or s in maker_parts:
            continue
        out.append(s)
    # collapse blank runs
    res = []
    for s in out:
        if s == "" and (not res or res[-1] == ""):
            continue
        res.append(s)
    return res


def is_heading(lines, i):
    s = lines[i]
    if not (3 <= len(s) <= 62) or len(s.split()) > 9:
        return False
    if re.search(r"[.!?] [A-ZÅÄÖ]|\b(19|20)\d\d\b|\[|\]|…", s):
        return False
    if not re.match(r"^(\d{1,2}(\.\d{1,2})*\.?\s+)?[A-ZÅÄÖ]", s):
        return False
    if s.endswith((".", ",", ";", ":", "–", "-", "?", "!", ")", "”", '"')):
        return False
    if re.search(r"\b(och|eller|att|av|i|på|för|med|som|till|en|ett|den|det|vid)$", s):
        return False
    if re.match(r"^(Jag|JO|Det|Den|De|Han|Hon|Enligt|I|Av|Vid|Om|Se)\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+", s) \
            and not ASSESS_HEAD.search(s):
        return False
    # previous content line must end a sentence (or be blank / a heading)
    j = i - 1
    while j >= 0 and lines[j] == "":
        j -= 1
    if j >= 0 and i - j == 1:
        p = lines[j]
        if not p.endswith(SENT_END) and not (len(p) <= 62 and is_heading(lines, j)):
            return False
    # next content line must start like a new sentence
    k = i + 1
    while k < len(lines) and lines[k] == "":
        k += 1
    if k >= len(lines):
        return False
    n = lines[k]
    if not re.match(r"^[A-ZÅÄÖ0-9”\"–\-•(]", n):
        return False
    return True


def unwrap(lines):
    """Join hard-wrapped lines into paragraphs."""
    paras, cur = [], ""
    for s in lines:
        if s == "":
            if cur:
                paras.append(cur)
                cur = ""
            continue
        if cur and (cur.endswith((".", "!", "?", ":")) and re.match(r"^[A-ZÅÄÖ0-9”\"–•]", s)
                    and len(cur) - cur.rfind("\n") > 0 and len(s) > 0
                    and len(cur.split("\n")[-1]) < 60):
            paras.append(cur)
            cur = s
        elif cur:
            if cur.endswith("-") and not cur.endswith(" -") and s[:1].islower():
                cur = cur[:-1] + s if re.search(r"[a-zåäö]-$", cur) and False else cur + s
            else:
                cur += " " + s
        else:
            cur = s
    if cur:
        paras.append(cur)
    return "\n".join(paras)


SIGN_RE = re.compile(r"^(Beslutet har fattats av|I beslutet har deltagit|Föredragande har varit|"
                     r"Ärendet har föredragits av|(Seniora? )?[Rr]ättssakkunnig\w* [A-ZÅÄÖ]|"
                     r"(Byrå|Avdelnings|Enhets|Områdes)chefen [A-ZÅÄÖ]\S+ [A-ZÅÄÖ]\S+ har)")


def cut_signature(lines):
    """Drop JO's signature block (decision maker / rapporteur) at the end."""
    lo = max(0, len(lines) - 14)
    for i in range(lo, len(lines)):
        if SIGN_RE.match(lines[i]):
            return lines[:i]
    return lines


def split_sections(lines, title=""):
    """Returns (preamble_lines, [(heading, [lines])])."""
    tnorm = re.sub(r"\s+", " ", title)
    heads = [i for i in range(len(lines)) if is_heading(lines, i)
             and not (len(lines[i]) > 25 and lines[i] in tnorm)]
    if not heads:
        return lines, []
    # the first *real* heading: first one matching a known section word, else
    # the first heading after line 3 (skips the multi-line title)
    start = None
    for i in heads:
        if FACT_HEAD.match(lines[i]) or ASSESS_HEAD.search(lines[i]) or LEGAL_HEAD.match(lines[i]):
            start = i
            break
    if start is None:
        return lines, []
    heads = [i for i in heads if i >= start]
    secs = []
    for n, i in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        secs.append((lines[i], lines[i + 1:end]))
    return lines[:start], secs


TAIL_RE = re.compile(
    r"\n?(Beslutet har fattats av|I beslutet har deltagit|Föredragande har varit|"
    r"Ärendet har föredragits av)[\s\S]*$")

PROV_RE = re.compile(
    r"\b(\d+\s?[a-z]?\s+kap\.\s+)?(\d+\s?[a-z]?(?:\s?(?:–|-|och|eller|,)\s?\d+\s?[a-z]?)*)\s+§§?"
    r"(\s+(?:första|andra|tredje|fjärde|femte)\s+stycket)?"
    r"\s+((?:[a-zåäö\-]+(?:lagen|balken|förordningen|ordningen|stadgan|formen)"
    r"(?:\s+\(\d{4}:\d+\))?)|(?:lagen|förordningen)\s+\(\d{4}:\d+\)[^.,;()]{0,80}?(?=[.,;()]|\s+(?:ska|får|har|är|och|framgår|följer|gäller|kan)\b)|"
    r"(?:FL|TF|RF|OSL|RB|BrB|SoL|LVU|LVM|LSS|PL|FB|SFB|HSL|PBL|KL|UtlL|FPL|LPT|LRV|JB|ÄB|YGL|HäL|FäL|FäF|HäF|PSL|SkolL|UB|FUK)\b)")

ABBREV = {"FL": "förvaltningslagen", "TF": "tryckfrihetsförordningen", "RF": "regeringsformen",
          "OSL": "offentlighets- och sekretesslagen", "RB": "rättegångsbalken"}


def find_provisions(text):
    seen, out = set(), []
    flat = re.sub(r"\s+", " ", text)
    for m in PROV_RE.finditer(flat):
        chap = (m.group(1) or "").strip()
        par = re.sub(r"\s+", " ", m.group(2)).strip()
        law = m.group(4).strip()
        s = ("%s %s § %s" % (chap, par, law)).strip() if chap else "%s § %s" % (par, law)
        if "§§" in m.group(0):
            s = s.replace(" § ", " §§ ", 1)
        s = re.sub(r"\s+", " ", s)
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
        if len(out) >= 60:
            break
    return out


# ---- outcome -------------------------------------------------------------
NEG = (r"(inte|ingen|inget|inga|saknas|saknar|utan|undgå[rt]?|avstår|avstå|underlåter|"
       r"tillräckligt? (?:skäl|grund|underlag|anledning)|kan inte|går inte|knappast|"
       r"inte tillräcklig\w*|finner inte|funnit inte|inte funnit|inte finner)")
SERIOUS_RE = re.compile(
    r"(allvarlig\w* kritik|kritiseras? allvarligt|allvarligt kritiser\w+|"
    r"mycket allvarlig\w* (?:på|kritik)|synnerligen allvarlig\w* kritik)", re.I)
CRIT_RE = re.compile(
    r"(får kritik|får även kritik|får också kritik|kritiseras|kritiserar|förtjänar kritik|"
    r"förtjänar (?:därför |också |även |alltså |således )?kritik|"
    r"ska (?:därför |också |även |alltså )?kritiseras|inte undgå kritik|"
    r"kan inte undgå kritik|undgår inte kritik|är (?:mycket |starkt |synnerligen )?kritisk\w* (?:till|mot)|"
    r"\bkritik mot\b|riktar (?:jag |JO )?(?:därför |också )?kritik|(?:^|\s)kritik för\b|"
    r"mycket kritisk|uttalar (?:därför |också )?kritik|finns (?:det )?(?:därför |alltså )?(?:skäl|anledning|grund) (?:att|till|för) (?:kritisera|kritik)|"
    r"anledning att (?:rikta )?kritik|är kritisk\w* till)", re.I)
NOCRIT_RE = re.compile(
    r"((?:inte|ingen|inget) (?:funnit |finner |finns |ser )?(?:någon |något |tillräcklig\w* |tillräckligt |heller |nu |därför )*"
    r"(?:skäl|anledning|grund|underlag|förutsättningar)[^.]{0,40}?(?:kritik|kritisera)|"
    r"ingen kritik|inte (?:kan |ska |bör )?kritisera[sr]?|undgår (?:med tvekan |därför |nätt och jämnt )?kritik|"
    r"inte (?:någon |något )?(?:skäl|anledning|grund) (?:att|för|till) (?:rikta |uttala )?(?:någon )?kritik|"
    r"föranleder (?:inte|ingen) (?:någon )?(?:kritik|åtgärd|ytterligare åtgärd|vidare åtgärd|något uttalande)|"
    r"inte (?:föranleder|ger anledning till) (?:någon )?(?:kritik|åtgärd)|"
    r"inte (?:agerat|handlat|gjort|förfarit) fel|kan inte kritiseras|"
    r"avstår (?:därför )?från (?:att rikta |att uttala )?kritik|stannar vid (?:dessa|de) uttalanden|"
    r"\b(?:inte|ingen|inget|inga)\b[^.]{0,120}\b(?:som|vilket) (?:förtjänar|motiverar|föranleder|ger anledning till|bör föranleda)[^.]{0,25}kritik|"
    r"\b(?:inte|ingen|inget|inga)\b(?:(?!därför|alltså|således|men |dock)[^.]){0,160}\b(?:stöd|underlag|grund|skäl|anledning|belägg)\b[^.,;]{0,50}(?:kritik|kritisera)|"
    r"i stället för att (?:uttala|rikta) kritik)", re.I)
STMT_RE = re.compile(r"(^uttalande|^fråga om|^frågor om|^om\b|uttalanden om|uttalar sig|JO uttalar|"
                     r"gör (?:vissa |några )?uttalanden|^initiativ|^granskning|^en granskning|"
                     r"^inspektion|^undersökning)", re.I)


def sentences(text):
    flat = re.sub(r"\s+", " ", text)
    flat = re.sub(r"\b(bl\.a|t\.ex|dvs|s\.k|m\.m|m\.fl|jfr|prop|kap|st|nr|dnr|f|ff|s|p|o\.d|osv|resp|"
                  r"t\.o\.m|fr\.o\.m|e\.d|ref|not|bet|rskr|skr)\.", lambda m: m.group(0).replace(".", "\x00"), flat)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÅÄÖ”\"])", flat)
    return [p.replace("\x00", ".").strip() for p in parts if p.strip()]


PAST_RE = re.compile(
    r"(\bdnr\b|JO (?:19|20)\d\d(?:/\d\d)? s\.|tidigare|i (?:ett |två |flera )?beslut den|så sent som|"
    r"återkommande|vid (?:flera|upprepade) tillfällen|flera gånger|redan (?:har|hade)|"
    r"riktade (?:jag|JO|chefsJO)|kritiserade (?:jag|JO|chefsJO)|uttalade (?:jag|JO|chefsJO)|"
    r"(?:har|hade) (?:jag|JO|chefsJO) (?:\w+ )?(?:kritiserat|riktat|uttalat)|"
    r"fick (?:då |även |också )?(?:allvarlig )?kritik|(?:har|hade) (?:hon|han|de|den|\w+en) (?:\w+ )?fått (?:allvarlig )?kritik|kritiserades|ånyo|på nytt|än en gång|"
    r"förtjänade (?:allvarlig )?kritik|ansåg jag|jag ansåg|frågan (?:är|var) (?:då |nu |alltså )?om|"
    r"\b(?:JK|Justitiekanslern|CPT|IVO|Inspektionen för vård och omsorg|kommittén|Riksrevisionen|Lagrådet|"
    r"Europarådet|FN:s \w+|revisorerna|länsstyrelsen|tingsrätten|hovrätten|förvaltningsrätten|kammarrätten)\b"
    r"[^.]{0,60}(?:kritiser|\bkritik\b|kritisk)|"
    r"anmälaren (?:är|var) kritisk|kritisk\w* (?:till|mot) (?:hur |att )?JO\b|"
    r"(?:anmälaren|han|hon|AA|BB|CC|de|nämnden|myndigheten|[A-ZÅÄÖ]\w+verket) "
    r"(?:har |hade )?(?:kritiserade|kritiserat|riktade kritik|riktat kritik|framförde kritik|framfört kritik|var kritisk\w*|är kritisk\w*))",
    re.I)
SOFT_NEG_RE = re.compile(
    r"(bekymmersam|oroande|oacceptab|inte godtagbar|inte acceptab|otillfredsställande|anmärkningsvär|"
    r"borde (?:ha|inte)|brist(?:er|en|erna|fällig)|allvarlig|felaktig|i strid med|saknade (?:lag)?stöd|"
    r"dröjsmål|långsam|fel\b|ifrågasätt|olämplig|inte förenlig)", re.I)
OPINION_RE = re.compile(
    r"(JO|chefsJO|ChefsJO) (uttalar|anser|konstaterar|framhåller|understryker|betonar|pekar|"
    r"gör|redogör|ifrågasätter|uppmärksammar|förutsätter|utgår|noterar|håller|ser|menar|påpekar)|"
    r"[Ee]nligt (JO|chefsJO)|uttalanden|uttalar sig", re.I)


def classify_sentence(s, allow_past=False):
    """-> 'serious' | 'crit' | 'nocrit' | None"""
    if s.rstrip().endswith("?"):
        return None
    if not allow_past and PAST_RE.search(s):
        return None
    if NOCRIT_RE.search(s):
        return "nocrit"
    ms = SERIOUS_RE.search(s)
    mc = CRIT_RE.search(s)
    m = ms or mc
    if not m:
        return None
    pre = s[max(0, m.start() - 70):m.start()]
    if re.search(r"\b" + NEG + r"\b[^,;]{0,60}$", pre, re.I):
        return "nocrit"
    return "serious" if ms else "crit"


def derive_outcome(title, summary, concl_text):
    """Conservative label from the title, the published summary and JO's own
    assessment. Returns (outcome, outcome_raw)."""
    found = {"serious": [], "crit": [], "nocrit": []}
    where = {"serious": set(), "crit": set(), "nocrit": set()}
    tl = (title or "").strip()
    c = classify_sentence(tl, allow_past=True) if "kriti" in tl.lower() else None
    if c:
        found[c].append(tl)
        where[c].add("title")
    for name, src in (("summary", summary or ""), ("text", concl_text or "")):
        for s in sentences(src):
            if "kriti" not in s.lower() and not NOCRIT_RE.search(s):
                continue
            c = classify_sentence(s)
            if c:
                where[c].add(name)
                if s not in found[c]:
                    found[c].append(s)
    summ = summary or ""
    raw = []
    if found["serious"]:
        out, raw = "allvarlig_kritik", found["serious"] + found["crit"]
    elif found["crit"]:
        out, raw = "kritik", found["crit"]
    elif found["nocrit"] and (where["nocrit"] & {"title", "summary"} or not SOFT_NEG_RE.search(summ)):
        out, raw = "ingen_kritik", found["nocrit"]
    elif STMT_RE.search(tl) or OPINION_RE.search(tl + " " + summ):
        out = "uttalande"
        raw = [s for s in sentences(summ) if OPINION_RE.search(s)][:3] or [tl]
    else:
        out = "unknown"
    return out, " ".join(raw[:4])[:2000]


# ---- agency --------------------------------------------------------------
STATE_AGENCIES = [
    "Försäkringskassan", "Migrationsverket", "Kriminalvården", "Polismyndigheten",
    "Skatteverket", "Centrala studiestödsnämnden", "CSN", "Arbetsförmedlingen",
    "Kronofogdemyndigheten", "Kronofogden", "Åklagarmyndigheten", "Ekobrottsmyndigheten",
    "Säkerhetspolisen", "Statens institutionsstyrelse", "SiS", "Pensionsmyndigheten",
    "Transportstyrelsen", "Trafikverket", "Inspektionen för vård och omsorg", "IVO",
    "Socialstyrelsen", "Skolinspektionen", "Skolverket", "Tullverket", "Försvarsmakten",
    "Lantmäteriet", "Bolagsverket", "Jordbruksverket", "Naturvårdsverket", "Konsumentverket",
    "Rättsmedicinalverket", "Domstolsverket", "Kustbevakningen", "Datainspektionen",
    "Integritetsskyddsmyndigheten", "Arbetsmiljöverket", "Diskrimineringsombudsmannen",
    "Justitiekanslern", "Regeringskansliet", "Utrikesdepartementet", "Boverket",
    "Livsmedelsverket", "Läkemedelsverket", "Folkhälsomyndigheten", "Myndigheten för samhällsskydd och beredskap",
    "Överförmyndare", "Universitets- och högskolerådet", "Universitetskanslersämbetet",
    "Statens servicecenter", "Kammarkollegiet", "Riksarkivet", "Sveriges Television",
    "Rekryteringsmyndigheten", "Plikt- och prövningsverket", "Totalförsvarets", "Sjöfartsverket",
    "Spelinspektionen", "Finansinspektionen", "Post- och telestyrelsen", "Energimyndigheten",
    "Statens skolverk", "Specialpedagogiska skolmyndigheten", "Rikspolisstyrelsen",
    "Inspektionen för arbetslöshetsförsäkringen", "Inspektionen för socialförsäkringen",
    "Sametinget", "Riksgälden", "Trafikanalys", "Tillväxtverket", "Statens jordbruksverk",
    "Riksdagsförvaltningen", "Riksbanken", "Riksrevisionen", "Svenska kraftnät", "Elsäkerhetsverket",
    "Skogsstyrelsen", "Havs- och vattenmyndigheten", "Strålsäkerhetsmyndigheten", "Kemikalieinspektionen",
    "Statens fastighetsverk", "Fortifikationsverket", "Myndigheten för press, radio och tv",
    "Granskningsnämnden", "Brottsoffermyndigheten", "Brottsförebyggande rådet", "Statens haverikommission",
    "E-hälsomyndigheten", "Tandvårds- och läkemedelsförmånsverket", "Myndigheten för yrkeshögskolan",
    "Arbetsdomstolen", "Revisorsinspektionen", "Fastighetsmäklarinspektionen", "Upphandlingsmyndigheten",
    "Konkurrensverket", "Patent- och registreringsverket", "Statistiska centralbyrån", "Valmyndigheten",
    "Statens tjänstepensionsverk", "Arbetsgivarverket", "Ekonomistyrningsverket", "Statskontoret",
    "Myndigheten för delaktighet", "Barnombudsmannen", "Institutet för språk och folkminnen",
    "Överklagandenämnden för studiestöd", "Överklagandenämnden för högskolan", "Skolväsendets överklagandenämnd",
    "Hälso- och sjukvårdens ansvarsnämnd", "Rättshjälpsmyndigheten", "Säkerhets- och integritetsskyddsnämnden",
    "Advokatsamfundet", "Notarienämnden", "Kriminalvårdsstyrelsen", "Vägverket", "Banverket",
    "Luftfartsverket", "Luftfartsstyrelsen", "Migrationsöverdomstolen",
    "Försvarets materielverk", "Forum för levande historia", "Allmänna reklamationsnämnden",
    "Ansvarsnämnden för djurens hälso- och sjukvård", "Statens ansvarsnämnd", "Polisen",
    "Myndigheten för stöd till trossamfund", "Svenska ESF-rådet", "Statens kulturråd",
    "Etikprövningsmyndigheten", "Överklagandenämnden för etikprövning", "Säkerhetspolisen",
    "Jämställdhetsmyndigheten", "Myndigheten för digital förvaltning", "Trafikförsäkringsföreningen",
]
AGENCY_PATTERNS = [
    # municipal boards / municipalities
    r"(?:[A-ZÅÄÖ][\wåäöÅÄÖ\-]*(?:nämnden?|styrelsen|förvaltningen|fullmäktige|kontoret)(?:\s+[A-ZÅÄÖ][\wåäö\-]+){0,3}\s+i\s+[A-ZÅÄÖ][\wåäöÅÄÖ\-]+(?:\s+[A-ZÅÄÖ]?[\wåäö\-]+)?\s+(?:kommun|stad|stadsdel\w*))",
    r"(?:[A-ZÅÄÖ][\wåäöÅÄÖ\-]*(?:\s+och\s+[\wåäö\-]+)?(?:\s+[a-zåäö\-]+)?(?:(?<=nämnd)|(?<=nämnden)|(?<=styrelsen))\s+i\s+[A-ZÅÄÖ][\wåäöÅÄÖ\-]+(?:\s+(?:kommun|stad))?)",
    r"(?:[A-ZÅÄÖ][\wåäöÅÄÖ\-]*(?:nämnden?|styrelsen|förvaltningen)\s+i\s+(?:Region|Landstinget)\s+[A-ZÅÄÖ][\wåäöÅÄÖ\-]+(?:\s+[A-ZÅÄÖ][\wåäö\-]+)?(?:\s+län)?)",
    r"(?:Överförmyndar\w+(?:\s+i\s+[A-ZÅÄÖ][\wåäöÅÄÖ\-]+(?:\s+(?:kommun|stad))?)?)",
    r"(?:Kommunstyrelsen\s+i\s+[A-ZÅÄÖ][\wåäöÅÄÖ\-]+(?:\s+(?:kommun|stad))?)",
    # courts
    r"(?:(?:[A-ZÅÄÖ][\wåäöÅÄÖ\-]+\s+){1,2}tingsrätt)",
    r"(?:Förvaltningsrätten\s+i\s+[A-ZÅÄÖ][\wåäöÅÄÖ\-]+)",
    r"(?:Kammarrätten\s+i\s+[A-ZÅÄÖ][\wåäöÅÄÖ\-]+)",
    r"(?:(?:Svea hovrätt|Göta hovrätt|Hovrätten\s+(?:för|över)\s+[A-ZÅÄÖ][\wåäöÅÄÖ\- ]+?(?=\s+för|\s+bl|,|\.|$)))",
    r"(?:Hyres- och arrendenämnden\s+i\s+[A-ZÅÄÖ][\wåäö\-]+|Hyresnämnden\s+i\s+[A-ZÅÄÖ][\wåäö\-]+)",
    r"(?:(?:Högsta förvaltningsdomstolen|Högsta domstolen|Migrationsdomstolen|Mark- och miljödomstolen|Patent- och marknadsdomstolen))",
    # regions, counties, universities
    r"(?:Länsstyrelsen\s+i\s+[A-ZÅÄÖ][\wåäöÅÄÖ\-]+(?:\s+[A-ZÅÄÖ]?[\wåäö\-]+)?\s+län|Länsstyrelsen\s+[A-ZÅÄÖ][\wåäö\-]+)",
    r"(?:Region\s+[A-ZÅÄÖ][\wåäöÅÄÖ\-]+(?:\s+(?:län|Götaland|Götalandsregionen))?|[A-ZÅÄÖ][\wåäö\-]+s\s+läns\s+landsting|Västra Götalandsregionen|Landstinget\s+(?:i\s+)?[A-ZÅÄÖ][\wåäö\-]+(?:\s+län)?)",
    r"(?:[A-ZÅÄÖ][\wåäöÅÄÖ\-]+\s+universitet|Universitetet\s+i\s+[A-ZÅÄÖ]\w+|Högskolan\s+(?:i\s+)?[A-ZÅÄÖ][\wåäö\-]+|Kungl(?:\.|iga)\s+Tekniska\s+högskolan|Karolinska\s+institutet|Sveriges lantbruksuniversitet|[A-ZÅÄÖ][\wåäö\-]+\s+högskola)",
    r"(?:[A-ZÅÄÖ][\wåäöÅÄÖ\-]+\s+[Uu]niversitetssjukhus\w*|[A-ZÅÄÖ][\wåäöÅÄÖ\-]+\s+lasarett|[A-ZÅÄÖ][\wåäö\-]+sjukhuset(?:\s+i\s+[A-ZÅÄÖ]\w+)?)",
    r"(?:Arbetslöshetskassan\s+[A-ZÅÄÖ][\wåäö\-]+|[A-ZÅÄÖ][\wåäö\-]+\s+arbetslöshetskassa|[A-ZÅÄÖ][\wåäö\-]+s\s+a-kassa)",
    r"(?:[A-ZÅÄÖ][\wåäöÅÄÖ\-]+\s+(?:kommun|stad)(?=\s|,|\.|$))",
]
AGENCY_RE = re.compile("|".join(AGENCY_PATTERNS))
STATE_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(a) for a in STATE_AGENCIES), key=len, reverse=True)) + r")(?=s?\b)")
BAD_AGENCY_START = re.compile(r"^(Kritik|Allvarlig|Uttalanden?|Fråga|Frågor|Även|Initiativ|En|Ett|Den|Det|JO|Om|"
                              r"Beslut|Anmälan|Granskning|Inspektion|Mycket|Viss|Vissa|Bl)\b")


def find_agency(title, summary, body_head):
    """Agency 'as written': first authority named in the title, then the
    published summary, then the start of the decision text."""
    for src in (title, summary, body_head):
        if not src:
            continue
        src = re.sub(r"\s+", " ", src)
        cands = []
        m = re.search(r"[Kk]ritik (?:mot|till)\s+(?:en |ett |två |tre |flera |dåvarande |den |tidigare )?", src)
        scope = src[m.end():] if m else src
        for text in ([scope, src] if m else [src]):
            for mm in AGENCY_RE.finditer(text):
                cands.append((mm.start(), mm.group(0)))
            for mm in STATE_RE.finditer(text):
                cands.append((mm.start(), mm.group(0)))
            cands = [(p, c.strip(" ,.")) for p, c in cands if not BAD_AGENCY_START.match(c.strip())]
            if cands:
                cands.sort(key=lambda x: (x[0], -len(x[1])))
                return cands[0][1]
    return ""


CATEGORY_TYPE = {
    "kriminalvård": "statlig myndighet", "polis": "statlig myndighet",
    "åklagarväsendet": "statlig myndighet", "socialförsäkring": "statlig myndighet",
    "migration": "statlig myndighet", "utlänningsärenden": "statlig myndighet",
    "skatt och folkbokföring": "statlig myndighet", "skatt": "statlig myndighet",
    "arbetsmarknad": "statlig myndighet", "exekutiva ärenden": "statlig myndighet",
    "försvar": "statlig myndighet", "tull": "statlig myndighet",
    "socialtjänst och lss": "kommun", "socialtjänst": "kommun",
    "överförmyndare": "kommun", "plan- och bygg": "kommun",
    "miljö- och hälsoskydd": "kommun", "kommunal förvaltning": "kommun",
    "allmänna domstolar": "domstol", "förvaltningsdomstolar": "domstol", "domstolar": "domstol",
}


def agency_type(agency, categories):
    a = agency or ""
    if a:
        if re.search(r"tingsrätt|hovrätt|[Ff]örvaltningsrätten|[Kk]ammarrätten|domstol|"
                     r"[Hh]yresnämnden|[Hh]yres- och arrendenämnden", a) and "Domstolsverket" not in a:
            return "domstol"
        if re.search(r"\bRegion\b|[Ll]andsting|Götalandsregionen", a):
            return "region"
        if re.search(r"\bkommun\b|\bstad\b|stadsdel|[Öö]verförmyndar|[Kk]ommunstyrelsen|nämnd\w*\s+i\s+", a):
            return "kommun"
        if a in ("Advokatsamfundet", "Sveriges Television", "Trafikförsäkringsföreningen") \
                or re.search(r"[Aa]rbetslöshetskassa|a-kassa", a):
            return "annat"
        if re.search(r"sjukhus|lasarett", a):
            return "region"
        if STATE_RE.search(a) or re.search(r"Länsstyrelsen|universitet|[Hh]ögskola|institutet", a):
            return "statlig myndighet"
    types = set()
    for c in categories or []:
        t = CATEGORY_TYPE.get(c.strip().lower())
        if t:
            types.add(t)
    if len(types) == 1:
        return list(types)[0]
    return "unknown"


# ---- named officials -----------------------------------------------------
ROLE = (r"(handläggaren|handläggare|enhetschefen|sektionschefen|avdelningschefen|områdeschefen|"
        r"kammarchefen|chefsåklagaren|kammaråklagaren|åklagaren|rådmannen|lagmannen|chefsrådmannen|"
        r"domaren|tingsfiskalen|tingsnotarien|hovrättsrådet|kammarrättsrådet|förvaltningsrättsfiskalen|"
        r"polisinspektören|polisassistenten|kommissarien|polismästaren|polisintendenten|polisen|"
        r"kriminalvårdsinspektören|kriminalvårdschefen|anstaltschefen|kronofogden|kronoinspektören|"
        r"socialsekreteraren|socialchefen|förvaltningschefen|kommundirektören|ordföranden|ordförande|"
        r"generaldirektören|rektorn|överläkaren|chefsöverläkaren|läkaren|juristen|chefsjuristen|"
        r"verksjuristen|registratorn|tjänstemannen|inspektören|stadsbyggnadschefen|byggnadsinspektören|"
        r"kommunalrådet|kommunjuristen|överförmyndaren|vårdaren|kriminalvårdaren|"
        r"vice riksåklagaren|riksåklagaren|överåklagaren|rikspolischefen|regionpolischefen|"
        r"skattehandläggaren|migrationshandläggaren|beslutsfattaren|avdelningsdirektören|rättschefen)")
NAMED_RE = re.compile(r"\b" + ROLE + r"\s+([A-ZÅÄÖ][a-zåäöé]+(?:-[A-ZÅÄÖ][a-zåäöé]+)?)\s+"
                      r"((?:(?:af|von|de|van)\s+)?[A-ZÅÄÖ][a-zåäöé]+(?:-[A-ZÅÄÖ][a-zåäöé]+)?)\b")
ANON_RE = re.compile(r"\b(AA|BB|CC|DD|EE|FF|NN|A\.A\.|B\.B\.|C\.C\.)\b")
NOT_NAME = set("Stockholm Göteborg Malmö Uppsala Region Syd Väst Öst Nord Mitt Bergslagen Sverige "
               "Den Det De Han Hon Jag Vid Enligt Efter Under Som Har Hade Var Är Kan Ska I På Av "
               "Kumla Hall Saltvik Norrtälje Tidaholm Västra Södra Norra Östra City".split())


def names_officials(body):
    """True if the body names an individual official of the reviewed authority
    (role + first and last name). False if the decision visibly uses
    anonymised initials (AA, BB ...) and no such name. None otherwise."""
    for m in NAMED_RE.finditer(body):
        if m.group(2) in NOT_NAME or m.group(3) in NOT_NAME:
            continue
        return True
    if ANON_RE.search(body):
        return False
    return None


def strip_html(s):
    s = re.sub(r"<br\s*/?>|</p>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"[ \t]+", " ", html.unescape(s)).strip()


def parse_one(hit, raw):
    flat = raw.startswith("[FLAT]")
    title = re.sub(r"\s+", " ", html.unescape(hit.get("post_title") or "")).strip()
    summary = re.sub(r"\s*\n\s*", "\n", strip_html(hit.get("post_content")))
    maker = (hit.get("resolve_maker") or "").strip()
    cats = hit.get("authority_names") or []
    matters = hit.get("matter_of_fact_names") or []
    regs = hit.get("legal_regulation_names") or []

    sections = {}
    if flat:
        text = re.sub(r"\s+", " ", raw[6:]).strip()
        pre, secs = [], []
        body = text
    else:
        lines = clean_lines(raw, maker)
        pre, secs = split_sections(cut_signature(lines), title)
        text = unwrap(lines)
        body = unwrap([l for h, ls in secs for l in [h] + ls]) if secs else text
    body_main = TAIL_RE.sub("", body)

    order = []
    for h, ls in secs:
        key, n = h, 2
        while key in sections:
            key = "%s (%d)" % (h, n)
            n += 1
        sections[key] = unwrap(ls)
        order.append(key)

    # facts vs assessment: assessment begins at the first heading that looks
    # like JO's assessment and runs to the end of the decision
    a_idx = None
    for i, k in enumerate(order):
        if re.search(r"bedömning|överväganden|ställningstagande", k, re.I) and len(k) < 70:
            a_idx = i
            break
    facts_text = assess_text = ""
    if a_idx is not None:
        facts_text = "\n".join("%s\n%s" % (k, sections[k]) for k in order[:a_idx]).strip()
        assess_text = TAIL_RE.sub("", "\n".join(
            "%s\n%s" % (k, sections[k]) for k in order[a_idx:])).strip()
    separable = bool(a_idx is not None and len(facts_text) > 300 and len(assess_text) > 300)

    if not summary and not flat:
        m = re.search(r"Beslutet i korthet:\s*(.+)", unwrap(pre), re.S)
        if m:
            summary = m.group(1).strip()
    # conclusion region when there is no assessment heading: last part of body
    concl = assess_text if assess_text else body_main[-3500:]
    outcome, outcome_raw = derive_outcome(title, summary, concl)

    agency = find_agency(title, summary, body_main[:1500])
    rec = collections.OrderedDict()
    rec["source"] = "jo"
    rec["url"] = hit.get("permalink")
    rec["diarienummer"] = hit.get("diary_number")
    rec["date"] = hit.get("resolve_date")
    rec["title"] = title
    rec["summary"] = summary
    rec["agency"] = agency
    rec["agency_type"] = agency_type(agency, cats)
    rec["area"] = "; ".join(cats + matters)
    rec["provisions"] = find_provisions(text)
    rec["text"] = text
    rec["sections"] = sections
    rec["outcome_raw"] = outcome_raw
    rec["outcome"] = outcome
    rec["names_officials"] = names_officials(body_main)
    # extras (published metadata and derived helpers)
    rec["authority_category"] = cats
    rec["matters"] = matters
    rec["legal_regulations"] = regs
    rec["decision_maker"] = maker
    rec["pdf_url"] = hit.get("pdf_url")
    rec["facts_text"] = facts_text
    rec["assessment_text"] = assess_text
    rec["has_separate_assessment"] = separable
    rec["text_source"] = "search_api_flat" if flat else "pdf"
    return rec


def parse_all(limit):
    order = load_order()[:limit]
    n = 0
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w") as out:
        for entry in order:
            hid = entry["id"]
            hp = os.path.join(HITS_DIR, "%s.json" % hid)
            tp = os.path.join(TXT_DIR, "%s.txt" % hid)
            if not (os.path.exists(hp) and os.path.exists(tp)):
                continue
            with open(hp) as f:
                hit = json.load(f)
            with open(tp) as f:
                raw = f.read()
            rec = parse_one(hit, raw)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    os.replace(tmp, OUT_FILE)
    print("wrote %d records to %s" % (n, OUT_FILE))


def stats():
    n = 0
    oc, at, ag = collections.Counter(), collections.Counter(), collections.Counter()
    no, src = collections.Counter(), collections.Counter()
    dates, lens, sep = [], [], 0
    with open(OUT_FILE) as f:
        for line in f:
            r = json.loads(line)
            n += 1
            oc[r["outcome"]] += 1
            at[r["agency_type"]] += 1
            ag[r["agency"] or "(none)"] += 1
            no[str(r["names_officials"])] += 1
            src[r["text_source"]] += 1
            if r["date"]:
                dates.append(r["date"])
            lens.append(len(r["text"]))
            sep += 1 if r["has_separate_assessment"] else 0
    print("count:", n)
    print("outcome:", dict(oc.most_common()))
    print("agency_type:", dict(at.most_common()))
    print("top agencies:")
    for a, c in ag.most_common(16):
        print("   %4d  %s" % (c, a))
    print("date range:", min(dates), "..", max(dates))
    print("median text length:", int(statistics.median(lens)))
    print("separable facts/assessment:", sep)
    print("names_officials:", dict(no))
    print("text source:", dict(src))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1500)
    ap.add_argument("--budget-mb", type=int, default=950)
    ap.add_argument("--parse-only", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.stats:
        stats()
        return
    if not a.parse_only:
        try:
            collect(a.limit, a.budget_mb * 1000 * 1000)
        except Blocked as e:
            print("STOPPED: %s" % e)
            parse_all(a.limit)
            sys.exit(2)
    parse_all(a.limit)
    stats()


if __name__ == "__main__":
    main()
