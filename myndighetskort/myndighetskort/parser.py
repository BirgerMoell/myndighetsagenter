"""Parse a regleringsbrev (Markdown from Öppna Statsliggaren) into an agency card.

Deterministic, regex based, no network and no model calls. The entry point is
`parse_letter(row, gazetteer)`, where `row` is a dict with the dataset columns
and `gazetteer` comes from `build_gazetteer(names)`.
"""
import re
import unicodedata

SCHEMA_VERSION = 1

MONTHS = {
    "januari": 1, "februari": 2, "mars": 3, "april": 4, "maj": 5, "juni": 6,
    "juli": 7, "augusti": 8, "september": 9, "oktober": 10, "november": 11,
    "december": 12,
}
_MONTH_RE = "|".join(MONTHS)

# ---------------------------------------------------------------- text utils

_KEEP_HYPHEN = {"och", "eller", "samt", "respektive", "som", "till", "i", "resp"}
_HYPH_RE = re.compile(r"([A-Za-zåäöéÅÄÖ]{2})- ([a-zåäöé]+)")


def dehyphenate(text):
    """Join words broken by the PDF line wrap ("Skatte- verket"), but keep
    real suspended hyphens ("verksamhets- och it-utveckling")."""
    def rep(m):
        if m.group(2) in _KEEP_HYPHEN:
            return m.group(0)
        return m.group(1) + m.group(2)
    return _HYPH_RE.sub(rep, text)


_NUMBERING_RE = re.compile(r"^\d{1,2}(\.\d{1,2})*\.?\s+(?=[A-ZÅÄÖ])")


def clean_title(raw):
    t = raw.strip().strip("#").strip()
    t = t.replace("\\*", "").strip("*").strip()
    t = _NUMBERING_RE.sub("", t)
    return t.strip(" :")


def slugify(name):
    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def to_int(cell):
    t = cell.replace(" ", "").replace(" ", "").replace("−", "-")
    t = t.replace("\\-", "-")
    t = re.sub(r"(tkr|kr)$", "", t)
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    return None


# -------------------------------------------------------------------- tables

def is_table_line(line):
    return line.lstrip().startswith("|")


def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def is_separator(cells):
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


# ------------------------------------------------------------------ headings

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

_STRUCTURAL = {
    "mål och återrapporteringskrav": "goals",
    "övriga mål och återrapporteringskrav": "goals",
    "verksamhetsstyrning": "goals",
    "organisationsstyrning": "org",
    "uppdrag": "assignments",
    "övriga uppdrag": "assignments",
}

_H2 = [
    ("verksamhet", "verksamhet"),
    ("finansiering", "finansiering"),
    ("avgifter och bidrag", "avgifter"),
    ("undantag från ekonomiadministrativa regelverket", "undantag"),
]


def _h2_key(title):
    low = title.lower()
    for prefix, key in _H2:
        if low.startswith(prefix):
            return key
    return None


_VAR_UO = re.compile(r"^Utgiftsområde\s+\d", re.I)
_VAR_APPR = re.compile(r"^\d{1,2}:\d{1,2}\s")
_VAR_AP = re.compile(r"^ap\.\s*\d", re.I)
_VAR_COND = re.compile(r"^Villkor för anslag\s+\d{1,2}:\d{1,2}\s*$", re.I)


def normalise_variant(md):
    """Letters decided in December 2024 were converted with another heading
    scheme (# VERKSAMHET, ## 1 Mål ..., ##### 1:1 Anslag ...). Rewrite them to
    the layout used everywhere else. Other letters pass through unchanged."""
    if not re.search(r"^#\s+(VERKSAMHET|FINANSIERING)\s*$", md, flags=re.M):
        return md, False
    out, block = [], None
    for line in md.split("\n"):
        s = line.strip()
        m = _HEADING_RE.match(s)
        if not m:
            if _VAR_COND.match(s):
                line = "**%s**" % s
            out.append(line)
            continue
        level, raw = len(m.group(1)), m.group(2).strip()
        title = clean_title(raw)
        if level == 1:
            key = _h2_key(title)
            if key:
                block = key
                out.append("## " + raw)
            else:
                out.append(line)
            continue
        if _VAR_UO.match(title):
            out.append("***%s***" % raw)
        elif _VAR_APPR.match(raw) or re.match(r"^Budgetår\s+\d{4}$", raw):
            out.append("**%s**" % raw)
        elif _VAR_AP.match(raw):
            out.append("***%s***" % raw)
        elif block == "verksamhet":
            if title.lower() in _STRUCTURAL:
                out.append("### " + raw)
            elif level >= 4:
                out.append("#### " + raw)
            else:
                out.append("***%s***" % raw)
        else:
            out.append("#" * min(level + 1, 6) + " " + raw)
    return "\n".join(out), True


def split_h2(md):
    """Return (preamble_lines, {key: lines}) for the top-level blocks."""
    pre, blocks, cur = [], {}, None
    for line in md.split("\n"):
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) <= 2:
            key = _h2_key(clean_title(m.group(2))) if len(m.group(1)) == 2 else None
            if len(m.group(1)) == 2 and key:
                cur = key
                blocks.setdefault(cur, [])
                continue
            if len(m.group(1)) == 2:
                cur = "other"
                blocks.setdefault(cur, []).append(line)
                continue
        if cur is None:
            pre.append(line)
        else:
            blocks[cur].append(line)
    return pre, blocks


# --------------------------------------------------------- item segmentation

_BI_RE = re.compile(r"^\*\*\*(.+?)\*\*\*:?$")
_B_RE = re.compile(r"^\*\*([^*].*?)\*\*:?$")
_DASH_RE = re.compile(r"^[—–]\s+")
_REPORT_TITLES = {"återrapportering", "återrapporteringskrav", "återrapportering m.m."}


def _title_of(line):
    """Return (rank, title) if the line is an item title, else None."""
    s = line.strip()
    if not s:
        return None
    m = _HEADING_RE.match(s)
    if m:
        return (1, clean_title(m.group(2)))
    m = _BI_RE.match(s)
    if m and len(s) <= 260:
        return (2, clean_title(m.group(1)))
    m = _B_RE.match(s)
    if m and len(s) <= 170 and not m.group(1).rstrip().endswith("."):
        return (3, clean_title(m.group(1)))
    return None


def segment(lines):
    """Split section lines into [{title, group, text}]."""
    blocks = [{"rank": 0, "title": None, "body": []}]
    for line in lines:
        t = _title_of(line)
        if t and t[1]:
            blocks.append({"rank": t[0], "title": t[1], "body": []})
        else:
            blocks[-1]["body"].append(line)
    items, stack = [], []
    for i, b in enumerate(blocks):
        text = "\n".join(b["body"]).strip()
        if b["rank"] == 0:
            if text:
                items.extend(_split_dashes(text))
            continue
        while stack and stack[-1][0] >= b["rank"]:
            stack.pop()
        group = [t for _, t in stack]
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if b["title"].lower() in _REPORT_TITLES and items and text:
            items[-1]["text"] += "\n\nÅterrapportering:\n" + text
            continue
        stack.append((b["rank"], b["title"]))
        if not text and nxt is not None and nxt["rank"] > b["rank"]:
            continue  # pure group heading
        items.append({"title": b["title"], "group": group, "text": text})
    return [it for it in items if it["title"] or it["text"]]


def _split_dashes(text):
    """An untitled block that is a list of em-dash bullets becomes one item
    per bullet; otherwise it stays one untitled item."""
    lines = text.split("\n")
    starts = [i for i, l in enumerate(lines) if _DASH_RE.match(l.strip())]
    if len(starts) < 2:
        return [{"title": None, "group": [], "text": text}]
    out = []
    lead = "\n".join(lines[:starts[0]]).strip()
    for j, s in enumerate(starts):
        e = starts[j + 1] if j + 1 < len(starts) else len(lines)
        body = _DASH_RE.sub("", "\n".join(lines[s:e]).strip())
        words = re.split(r"\s+", body)
        title = " ".join(words[:10]).rstrip(",.;:") + ("…" if len(words) > 10 else "")
        out.append({"title": title, "group": [], "text": body, "from_bullet": True})
    if lead and len(lead) > 200:
        out.insert(0, {"title": None, "group": [], "text": lead})
    return out


# ------------------------------------------------------------ item features

_DATE_RE = re.compile(
    r"(senast\s+(?:den\s+)?)?(?<!\d)(\d{1,2})\s+(%s)\s+(\d{4})" % _MONTH_RE, re.I)
_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DAYMONTH_RE = re.compile(r"(?<!\d)(\d{1,2})\s+(%s)(?!\s+\d{4})" % _MONTH_RE, re.I)
_RK_RE = re.compile(r"till\s+Regeringskansliet(?:\s*\(([^)]{3,140})\))?")
_ANSLAG_RE = re.compile(r"anslag(?:et|en)?\s+(\d{1,2}:\d{1,2})")
_UO_RE = re.compile(r"utgiftsområde\s+(\d{1,2})", re.I)
_SFS_RE = re.compile(r"\((\d{4}:\d{1,4})\)")
_BULLET_RE = re.compile(r"^\s*(?:[-–—•]|\d{1,2}[.)]|[a-z]\))\s+(.*)$")

TASK_TYPES = [
    ("rapportera", r"redovis|återrapport|rapportera"),
    ("prognos", r"prognos"),
    ("analysera", r"analyser|kartlägg|utvärder|följa upp|följ upp|uppföljning|bedöm"),
    ("föreslå", r"lämna förslag|föreslå|författningsförslag"),
    ("samordna", r"samordna|samverka|i dialog med|inhämta synpunkter|bistå|i samråd"),
    ("fördela_medel", r"fördela|betala ut|utbetala|bidrag till|statsbidrag|rekvir"),
    ("utveckla", r"utveckla|inrätta|införa|genomföra|bygga upp|förbereda"),
    ("tillsyn_kontroll", r"tillsyn|kontroll|granska|inspektion"),
    ("informera", r"informationsinsats|informera|sprida kunskap|kunskapshöjande|vägled"),
    ("statistik_data", r"statistik|datadriv|dataanalys|register|indikator"),
    ("digitalisering_ai", r"digitaliser|artificiell intelligens|\bai\b|it-utveckling"),
    ("beredskap_säkerhet", r"beredskap|totalförsvar|säkerhetsskydd|hybridhot|cybersäkerhet|informationssäkerhet"),
    ("eu_internationellt", r"\beu\b|eu:s|europeisk|internationell"),
]
_TASK_RES = [(k, re.compile(p)) for k, p in TASK_TYPES]

CHANNELS = [
    ("årsredovisning", r"årsredovisning"),
    ("hermes", r"\bhermes\b"),
    ("muntlig", r"muntlig"),
    ("delredovisning", r"delredovis|delrapport"),
    ("slutredovisning", r"slutredovis|slutrapport"),
    ("budgetunderlag", r"budgetunderlag"),
]
_CHANNEL_RES = [(k, re.compile(p)) for k, p in CHANNELS]

ALIASES = {
    "Skolverket": "Statens skolverk",
    "Skolinspektionen": "Statens skolinspektion",
    "Jordbruksverket": "Statens jordbruksverk",
    "Energimyndigheten": "Statens energimyndighet",
    "Kulturrådet": "Statens kulturråd",
    "Vinnova": "Verket för innovationssystem",
    "Sida": "Styrelsen för internationellt utvecklingssamarbete",
    "MSB": "Myndigheten för samhällsskydd och beredskap",
    "ESV": "Ekonomistyrningsverket",
    "SCB": "Statistiska centralbyrån",
    "Brå": "Brottsförebyggande rådet",
    "IVO": "Inspektionen för vård och omsorg",
    "CSN": "Centrala studiestödsnämnden",
    "Digg": "Myndigheten för digital förvaltning",
    "PTS": "Post- och telestyrelsen",
    "SMHI": "Sveriges meteorologiska och hydrologiska institut",
    "SGU": "Sveriges geologiska undersökning",
    "FOI": "Totalförsvarets forskningsinstitut",
    "FMV": "Försvarets materielverk",
    "FRA": "Försvarets radioanstalt",
    "Formas": "Forskningsrådet för miljö, areella näringar och samhällsbyggande",
    "Forte": "Forskningsrådet för hälsa, arbetsliv och välfärd",
    "SiS": "Statens institutionsstyrelse",
    "ISF": "Inspektionen för socialförsäkringen",
    "Svenska kraftnät": "Affärsverket svenska kraftnät",
    "Länsstyrelserna": "länsstyrelserna",
    "länsstyrelsen": "länsstyrelserna",
    "Länsstyrelsen": "länsstyrelserna",
    "SKR": "Sveriges Kommuner och Regioner",
    "Sveriges Kommuner och Regioner": "Sveriges Kommuner och Regioner",
    "Sveriges Kommuner och Landsting": "Sveriges Kommuner och Regioner",
    "arbetslöshetskassorna": "arbetslöshetskassorna",
    "Ekonomistyrningsverket": "Ekonomistyrningsverket",
}
_NOT_PARTNERS = {"Regeringskansliet", "regeringen", "Regeringen"}


def build_gazetteer(agency_names):
    """Names are matched as substrings (so genitive forms match); aliases need
    a word boundary because many are short."""
    names = sorted({n for n in agency_names if n and len(n) >= 6}, key=len, reverse=True)
    alias = {a: (re.compile(r"(?<![\wåäöÅÄÖ])%s(?:s|:s)?(?![\wåäöÅÄÖ])" % re.escape(a)), c)
             for a, c in ALIASES.items()}
    return {"names": names, "alias": alias}


def _mentions(text, gaz, candidates=None):
    found = []
    names = candidates if candidates is not None else gaz["names"]
    for n in names:
        if n in text:
            found.append(n)
    for a, (rx, canon) in gaz["alias"].items():
        if a in text and rx.search(text):
            found.append(canon)
    return found


def _iso(y, m, d):
    try:
        y, m, d = int(y), int(m), int(d)
        if 1 <= m <= 12 and 1 <= d <= 31:
            return "%04d-%02d-%02d" % (y, m, d)
    except ValueError:
        pass
    return None


def enrich(item, section, year, self_name, gaz, letter_names):
    text = item["text"]
    full = ((item["title"] or "") + "\n" + text)
    low = full.lower()
    dates, deadlines = [], []
    for m in _DATE_RE.finditer(text):
        iso = _iso(m.group(4), MONTHS[m.group(3).lower()], m.group(2))
        if iso:
            dates.append(iso)
            if m.group(1):
                deadlines.append(iso)
    for m in _ISO_RE.finditer(text):
        iso = _iso(*m.groups())
        if iso:
            dates.append(iso)
    item["dates"] = sorted(set(dates))
    item["deadlines"] = sorted(set(deadlines))
    if "prognos" in low and year:
        fd = set()
        for m in _DAYMONTH_RE.finditer(text):
            iso = _iso(year, MONTHS[m.group(2).lower()], m.group(1))
            if iso:
                fd.add(iso)
        item["forecast_dates"] = sorted(fd)
    recipients = []
    for m in _RK_RE.finditer(text):
        recipients.append("Regeringskansliet (%s)" % m.group(1).strip() if m.group(1)
                          else "Regeringskansliet")
    if re.search(r"till\s+regeringen\b", text):
        recipients.append("regeringen")
    mentioned = [n for n in _mentions(full, gaz, letter_names)
                 if n != self_name and n not in _NOT_PARTNERS]
    mentioned = sorted(set(mentioned))
    for n in mentioned:
        if re.search(r"till\s+%s" % re.escape(n), text):
            recipients.append(n)
    item["recipients"] = sorted(set(recipients))
    item["partners"] = mentioned
    item["channels"] = [k for k, rx in _CHANNEL_RES if rx.search(low)]
    item["task_types"] = [k for k, rx in _TASK_RES if rx.search(low)]
    item["anslag_refs"] = sorted(set(_ANSLAG_RE.findall(text)))
    item["expenditure_area_refs"] = sorted({int(x) for x in _UO_RE.findall(text)})
    item["sfs_refs"] = sorted(set(_SFS_RE.findall(text)))
    subs = []
    for line in text.split("\n"):
        m = _BULLET_RE.match(line)
        if m and len(m.group(1)) > 15:
            subs.append(m.group(1).strip())
    item["sub_questions"] = subs
    grp = " ".join(item["group"] + [item["title"] or ""]).lower()
    item["ongoing"] = bool(re.search(r"pågående|tidigare givna|återkommande", grp))
    item["section"] = section
    return item


# ------------------------------------------------------------------- finance

_APPR_RE = re.compile(r"^\*\*(\d{1,2}:\d{1,2})\s+(.+?)(?:\s*\(([^()]*anslag[^()]*)\))?\*\*$", re.I)
_COND_RE = re.compile(r"^\*\*Villkor för anslag\s+(\d{1,2}:\d{1,2})\*\*$", re.I)
_UOHEAD_RE = re.compile(r"^\*{2,3}Utgiftsområde\s+(\d{1,2})\s*(.*?)\*{2,3}$", re.I)
_BUDGETYEAR_RE = re.compile(r"^\*\*Budgetår\s+(\d{4})\*\*$", re.I)
_AP_RE = re.compile(r"^ap\.\s*(\d+(?:\.\d+)*)", re.I)
_APHEAD_RE = re.compile(r"^\*{2,3}ap\.\s*(\d+(?:\.\d+)*)\s*(.*?)\*{2,3}$", re.I)
_CAP_RE = re.compile(r"högst\s+([\d\u00a0 ]{1,20}\d)\s*(tkr|kronor|kr)\b", re.I)


def _last_int(cells):
    for c in reversed(cells):
        v = to_int(c)
        if v is not None:
            return v
    return None


def parse_finance(lines, warnings):
    out = {"appropriations": [], "financial_terms": [], "loans": {},
           "payment_plan": [], "tables": []}
    path = []           # heading path inside FINANSIERING
    older = False
    uo = None
    budget_year = None
    appr = None         # current appropriation dict
    disposal = None
    cond_code = None
    cond_ap = None
    by_code = {}
    table = None

    def flush_table():
        nonlocal table
        if table and table["rows"]:
            out["tables"].append(table)
        table = None

    in_appr = False
    saw_table = False
    for raw in lines:
        s = raw.strip()
        m = _HEADING_RE.match(s)
        if m:
            flush_table()
            level = len(m.group(1))
            title = clean_title(m.group(2))
            path = [p for p in path if p[0] < level] + [(level, title)]
            low = title.lower()
            in_appr = low.startswith("tilldelade anslag") or low.startswith("äldre anslag") \
                or (in_appr and level > 4)
            if low.startswith("tilldelade anslag") or low.startswith("äldre anslag"):
                older = low.startswith("äldre")
                appr = disposal = cond_code = cond_ap = None
                budget_year = None
            continue
        ptxt = " / ".join(t for _, t in path)
        plow = ptxt.lower()
        if in_appr:
            mu = _UOHEAD_RE.match(s)
            if mu:
                uo = {"number": int(mu.group(1)), "name": mu.group(2).strip()}
                cond_code = None
                continue
            mb = _BUDGETYEAR_RE.match(s)
            if mb:
                budget_year = int(mb.group(1))
                continue
            mc = _COND_RE.match(s)
            if mc:
                cond_code, cond_ap = mc.group(1), None
                continue
            ma = _APPR_RE.match(s)
            if ma:
                appr = {"code": ma.group(1), "name": ma.group(2).strip(),
                        "kind": ma.group(3), "expenditure_area": uo,
                        "older_budget_year": budget_year if older else None,
                        "disposals": [], "conditions": []}
                out["appropriations"].append(appr)
                by_code[(ma.group(1), budget_year if older else None)] = appr
                disposal = None
                cond_code = None
                continue
            if cond_code is not None:
                mh = _APHEAD_RE.match(s)
                if mh:
                    cond_ap = {"ap": mh.group(1), "name": mh.group(2).strip(), "text": ""}
                    target = by_code.get((cond_code, budget_year if older else None)) or appr
                    if target is not None:
                        target["conditions"].append(cond_ap)
                    continue
                if s and not is_table_line(s):
                    if cond_ap is None:
                        cond_ap = {"ap": None, "name": None, "text": ""}
                        target = by_code.get((cond_code, budget_year if older else None)) or appr
                        if target is not None:
                            target["conditions"].append(cond_ap)
                    cond_ap["text"] += (("\n" if cond_ap["text"] else "") + s)
                    continue
            if is_table_line(s):
                cells = split_row(s)
                if is_separator(cells) or not any(cells):
                    continue
                first = cells[0]
                saw_table = True
                if re.fullmatch(r"\d{1,2}:\d{1,2}", first) and len(cells) >= 2:
                    mk = re.search(r"\(([^()]*anslag[^()]*)\)\s*$", cells[1], re.I)
                    appr = {"code": first,
                            "name": re.sub(r"\s*\([^()]*anslag[^()]*\)\s*$", "", cells[1], flags=re.I),
                            "kind": mk.group(1) if mk else None, "expenditure_area": uo,
                            "older_budget_year": budget_year if older else None,
                            "disposals": [], "conditions": [],
                            "amount_tkr": _last_int(cells[2:])}
                    out["appropriations"].append(appr)
                    by_code[(first, budget_year if older else None)] = appr
                    disposal = None
                    continue
                dcell = next((c for c in cells if c.lower().startswith("disponeras av")), None)
                if dcell and not first and appr is not None:
                    disposal = {"by": dcell[len("disponeras av"):].strip(),
                                "amount_tkr": _last_int(cells) if _last_int(cells) is not None
                                else appr.get("amount_tkr"), "items": []}
                    appr["disposals"].append(disposal)
                    continue
                if first.lower().startswith("disponeras av") and appr is not None:
                    disposal = {"by": first[len("disponeras av"):].strip(),
                                "amount_tkr": _last_int(cells[1:]), "items": []}
                    appr["disposals"].append(disposal)
                    continue
                mp = _AP_RE.match(first)
                if mp and appr is not None:
                    if disposal is None:
                        disposal = {"by": None, "amount_tkr": None, "items": []}
                        appr["disposals"].append(disposal)
                    name = cells[1] if len(cells) > 2 else ""
                    disposal["items"].append({"ap": mp.group(1), "name": name,
                                              "amount_tkr": _last_int(cells[1:])})
                    continue
            continue
        # outside the appropriation lists
        if is_table_line(s):
            cells = split_row(s)
            if is_separator(cells):
                continue
            if table is None:
                table = {"path": ptxt, "rows": []}
            table["rows"].append(cells)
            if "utbetalningsplan" in plow:
                mi = _ISO_RE.search(cells[0])
                amt = _last_int(cells[1:])
                if mi and amt is not None:
                    out["payment_plan"].append({"date": mi.group(0), "amount_tkr": amt})
            elif "låneram" in plow and len(cells) >= 2 and cells[0]:
                amt = _last_int(cells[1:])
                if amt is not None:
                    out["loans"][cells[0]] = amt
        else:
            flush_table()
    flush_table()

    # financial terms: tables whose header has "Anslagskredit"
    for t in out["tables"]:
        rows = t["rows"]
        if not rows or not any("anslagskredit" in c.lower() for c in rows[0]):
            continue
        header, label = rows[0], None
        for r in rows[1:]:
            if not r or not r[0]:
                continue
            if _AP_RE.match(r[0]):
                rec = {"anslag": label, "ap": r[0]}
                for h, c in zip(header[1:], r[1:]):
                    v = to_int(c)
                    rec[h] = v if v is not None else (c or None)
                out["financial_terms"].append(rec)
            else:
                label = r[0]

    for a in out["appropriations"]:
        caps = []
        for c in a["conditions"]:
            c["text"] = dehyphenate(c["text"])
            for m in _CAP_RE.finditer(c["text"]):
                v = to_int(m.group(1))
                if v is not None:
                    caps.append({"ap": c["ap"], "max": v, "unit": m.group(2)})
        a["caps"] = caps
        for d in a["disposals"]:
            top = [i["amount_tkr"] for i in d["items"]
                   if i["amount_tkr"] is not None and "." not in i["ap"]]
            d["sum_ok"] = (sum(top) == d["amount_tkr"]) if (top and d["amount_tkr"] is not None) else None
    if saw_table and not out["appropriations"]:
        warnings.append("appropriations_not_parsed")
    return out


# -------------------------------------------------------------------- letter

_PROP_RE = re.compile(r"prop\.\s*(\d{4}/\d{2,4}:\d+)")
_BET_RE = re.compile(r"bet\.\s*(\d{4}/\d{2,4}[:/]\w+)")
_RSKR_RE = re.compile(r"rskr\.\s*(\d{4}/\d{2,4}:\d+)")
_BILAGOR_RE = re.compile(r"^(\d+)\s+bilag", re.M)


def parse_letter(row, gaz):
    """row: dict with id, original_rbid, type, date, year, category, name,
    department, expenditure_areas, registry_numbers, pdf, md."""
    warnings = []
    md, variant = normalise_variant(row.get("md") or "")
    md = dehyphenate(md)
    name = row.get("name")
    year = row.get("year")
    pre, blocks = split_h2(md)
    pre_text = "\n".join(pre)
    letter_names = [n for n in gaz["names"] if n in md]

    card = {
        "schema_version": SCHEMA_VERSION,
        "id": row.get("id"),
        "original_rbid": row.get("original_rbid"),
        "type": row.get("type"),
        "category": row.get("category"),
        "date": row.get("date"),
        "year": year,
        "name": name,
        "slug": slugify(name or ""),
        "department": row.get("department"),
        "expenditure_areas": list(row.get("expenditure_areas") or []),
        "registry_numbers": list(row.get("registry_numbers") or []),
        "pdf": row.get("pdf"),
        "riksdag_refs": {
            "prop": sorted(set(_PROP_RE.findall(pre_text))),
            "bet": sorted(set(_BET_RE.findall(pre_text))),
            "rskr": sorted(set(_RSKR_RE.findall(pre_text))),
        },
    }
    mb = _BILAGOR_RE.search(pre_text)
    card["n_attachments_stated"] = int(mb.group(1)) if mb else 0

    # VERKSAMHET
    sections = {"mission": [], "goals": [], "org": [], "assignments": []}
    cur = "mission"
    for line in blocks.get("verksamhet", []):
        m = _HEADING_RE.match(line.strip())
        if m and "*" not in m.group(2):
            key = _STRUCTURAL.get(clean_title(m.group(2)).lower())
            if key:
                cur = key
                continue
        sections[cur].append(line)
    card["mission"] = "\n".join(sections["mission"]).strip()
    for key in ("goals", "org", "assignments"):
        items = segment(sections[key])
        for n, it in enumerate(items):
            enrich(it, key, year, name, gaz, letter_names)
            it["item_id"] = "%s:%s:%d" % (row.get("id"), key, n)
        card[key] = items
    if "verksamhet" not in blocks:
        if row.get("category") == "Myndighet":
            warnings.append("no_verksamhet_block")
    elif not (card["goals"] or card["assignments"] or card["org"]):
        if len(card["mission"]) > 600:
            warnings.append("verksamhet_unstructured")

    # FINANSIERING
    fin = parse_finance(blocks.get("finansiering", []), warnings)
    card.update({k: fin[k] for k in ("appropriations", "financial_terms", "loans", "payment_plan")})
    card["finance_tables"] = fin["tables"]
    if "finansiering" not in blocks:
        warnings.append("no_finansiering_block")

    # AVGIFTER, UNDANTAG
    fee_w = []
    fees = parse_finance(blocks.get("avgifter", []), fee_w)
    card["fee_tables"] = fees["tables"]
    card["has_fee_section"] = "avgifter" in blocks
    card["exceptions_text"] = "\n".join(blocks.get("undantag", [])).strip()

    total = 0
    for a in card["appropriations"]:
        if a["older_budget_year"] is None:
            total += sum(d["amount_tkr"] or 0 for d in a["disposals"])
    all_dead = sorted({d for it in card["goals"] + card["org"] + card["assignments"]
                       for d in it["deadlines"]})
    partners = sorted({p for it in card["goals"] + card["org"] + card["assignments"]
                       for p in it["partners"]})
    card["summary"] = {
        "n_goals": len(card["goals"]),
        "n_org": len(card["org"]),
        "n_assignments": len(card["assignments"]),
        "n_deadlines": len(all_dead),
        "deadlines": all_dead,
        "partners": partners,
        "n_appropriations": len(card["appropriations"]),
        "appropriations_total_tkr": total,
        "payment_plan_total_tkr": sum(p["amount_tkr"] for p in card["payment_plan"]),
        "md_chars": len(md),
    }
    card["layout"] = "2024-12" if variant else "standard"
    card["warnings"] = warnings
    return card
