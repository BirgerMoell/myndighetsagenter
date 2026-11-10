"""Decision tasks built from real, published decisions.

Two sources (collected by the scripts in collect/):
  oks               Överklagandenämnden för studiestöd: appeals of CSN decisions.
                    Input: what the case is about + the "Ärendet" section (facts,
                    CSN's decision, the appeal). Label: the board's outcome and the
                    provisions it applied.
  skolinspektionen  Supervision decisions. Input: the case description and, per
                    assessed area, the section "Omständigheter som ligger till
                    grund för bedömningen". Label: deficiency per area and the
                    most severe intervention.

The label text (outcome, assessment, choice of intervention) is cut out of the
input; signature blocks, e-mail addresses, phone and organisation numbers are
removed. Labels are derived by rules from the decision text and are checked in
`audit`; anything unclear is dropped rather than guessed.
"""
import json
import re

from .schema import SYSTEM, extract_json, f1, task_row

# ------------------------------------------------------------- references

LAW_ALIASES = {
    "studiestödslagen": "1999:1395", "studiestödsförordningen": "2000:655",
    "förvaltningslagen": "2017:900", "skollagen": "2010:800", "skolförordningen": "2011:185",
    "gymnasieförordningen": "2010:2039", "utlänningslagen": "2005:716",
    "lagen om omställningsstudiestöd": "2022:856", "förordningen om omställningsstudiestöd": "2022:857",
    "socialförsäkringsbalken": "2010:110", "diskrimineringslagen": "2008:567",
}
_REF = re.compile(
    r"(?:(\d+)\s*([a-z])?\s*kap\.\s*)?(\d+)\s*([a-z])?\s*§+\s*"
    r"(?:(?:första|andra|tredje|fjärde|femte|sjätte)\s+(?:och\s+\w+\s+)?stycke[tn]\w*\s*)?(?:p\.\s*\d+\s*|\d+\s+)?"
    r"(?:i\s+)?((?:lagen|förordningen|förordning|lag)\s*\(\d{4}:\d+\)|(?:lagen|förordningen) om [a-zåäö]+"
    r"|[a-zåäö]+(?:lagen|förordningen|balken)|CSNFS\s*\d{4}:\d+)", re.I)


def refs(text):
    """Normalised statute references found in a text: 'kap:par:law'."""
    out = set()
    for m in _REF.finditer(text.replace("\n", " ")):
        law = re.sub(r"\s+", " ", m.group(5).lower())
        n = re.search(r"\d{4}:\d+", law)
        law = n.group(0) if n and "csnfs" not in law else LAW_ALIASES.get(law, law)
        out.add("%s:%s:%s" % ((m.group(1) or "") + (m.group(2) or ""), m.group(3) + (m.group(4) or ""), law))
    return out


def scrub(text):
    text = re.sub(r"\S+@\S+", "[e-post]", text)
    text = re.sub(r"\b\d{6}[-–]\d{4}\b", "[nummer]", text)
    text = re.sub(r"\b0\d{1,3}[- ]\d{2,3} ?\d{2,3} ?\d{2,3}\b", "[telefon]", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# -------------------------------------------------------------------- ÖKS

OKS_ENV = "oks_overklagande"
OKS_AGENCY = "Överklagandenämnden för studiestöd"
OKS_UTFALL = ["avslag", "bifall", "delvis_bifall", "aterforvisning", "avvisning"]
_OKS_FURNITURE = re.compile(
    r"\n(?:Postadress|Besöksadress|Telefon|E-post|Webbplats|Box \d+|\d{3} \d{2} \S+|Backgränd \d+|"
    r"010-\d[\d ]+|registrator@oks\.se|www\.oks\.se|ÖKS|Dnr:? ?[\d-]+|\d+ ?\(\d+\))[ \t]*(?=\n)")


def oks_outcome(beslut):
    """Outcome from the operative sentence(s). 'bifall' = changed in the appellant's
    favour, also when CSN is told to re-examine the rest on that basis."""
    b = re.sub(r"\s+", " ", beslut.lower())
    b = re.split(r"beslut (?:får|kan) (?:enligt|inte)", b)[0]
    avslar = bool(re.search(r"avslår (?:nn:s )?(?:överklagande|yrkande)|överklagande[tn]? avslås|avslås i övrigt|"
                            r"lämnar överklagandet utan bifall|överklagandet (?:lämnas|ska lämnas) utan bifall", b))
    change = bool(re.search(r"beslutar att|ändrar|bifaller|upphäver|undanröjer|förklarar att|"
                            r"med ändring av|med bifall till|har rätt till|ska inte krävas|nämnden finner att|"
                            r"beviljar|besluter|med upphävande", b))
    if re.search(r"återförvis|visar ärendet åter|återlämnar ärendet", b):
        return "aterforvisning"
    if avslar and change:
        return "delvis_bifall"
    if avslar:
        return "avslag"
    if re.search(r"avvisar|avvisas", b) and not change:
        return "avvisning"
    if re.search(r"avskriver|avskrivs", b) and not change:
        return None
    if change:
        return "delvis_bifall" if "delvis" in b else "bifall"
    return None


# Published decisions left out of the tasks because the fact section names a person:
# a private individual the source did not anonymise, or an outside expert.
OKS_EXCLUDE = {"2026-03992", "2015-01305"}


def oks_parse(rec):
    if rec.get("diarienummer") in OKS_EXCLUDE:
        return None
    t = rec["text"].replace("\x0c", "\n")
    t = _OKS_FURNITURE.sub("", "\n" + t)
    m = re.search(r"\n(?:Överklagandenämndens|Nämndens) beslut[ \t]*\n(.*?)\n[-\s]*Ärendet[ \t]*\n", t, re.S)
    a = re.search(r"\n[-\s]*Ärendet[ \t]*\n(.*?)\n\s*(?:Författningsbestämmelser|Tillämpliga bestämmelser|"
                  r"Bestämmelser|Skäl för beslutet|Skälen för beslutet)[ \t]*\n(.*)", t, re.S) \
        or re.search(r"\n[-\s]*Ärendet[ \t]*\n(.*?)(\n(?:Överklagandenämndens|Nämndens) bedömning[ \t]*\n.*)", t, re.S)
    if not m or not a:
        return None
    outcome = oks_outcome(m.group(1))
    saken = re.search(r"\nSaken[ \t]*\n(.*?)\n(?:Överklagandenämndens|Nämndens) beslut", t, re.S)
    rest = a.group(2)
    rest = re.split(r"\n[-\s]*Beslutet har fattats", rest)[0]
    bed = re.split(r"\n(?:Överklagandenämndens|Nämndens) bedömning[ \t]*\n", rest)
    facts = scrub(a.group(1))
    if not outcome or len(facts) < 300:
        return None
    return {
        "saken": re.sub(r"\s+", " ", saken.group(1)).strip() if saken else "",
        "arendet": facts,
        "utfall": outcome,
        "beslutsmening": re.sub(r"\s+", " ", m.group(1)).strip().split(" Överklagandenämndens beslut får")[0][:400],
        "lagrum": sorted(refs(rest)),
        "lagrum_alla": sorted(refs(rest)),
    }


OKS_FORMAT = """Svara med ett JSON-objekt:
{"utfall": "avslag" | "bifall" | "delvis_bifall" | "aterforvisning" | "avvisning",
 "lagrum": ["3 kap. 4 § studiestödslagen", ...],
 "motivering": "nämndens skäl i korthet"}
"bifall" betyder att det överklagade beslutet ändras till klagandens fördel."""


def oks_task(rec, n):
    p = oks_parse(rec)
    if not p:
        return None
    user = ("Ett beslut av Centrala studiestödsnämnden (CSN) har överklagats. Underlaget är hämtat ur ett "
            "offentliggjort, avidentifierat avgörande. Föreslå hur nämnden ska avgöra ärendet.\n\n"
            "Saken: %s\nOmråde: %s\n\nÄrendet:\n%s\n\n%s"
            % (p["saken"], rec.get("area") or "", p["arendet"], OKS_FORMAT))
    meta = {"oracle": {"utfall": p["utfall"], "lagrum": p["lagrum"]}, "lagrum_alla": p["lagrum_alla"],
            "beslutsmening": p["beslutsmening"], "diarienummer": rec.get("diarienummer"),
            "date": rec.get("date"), "url": rec.get("url"), "area": rec.get("area")}
    row = task_row(OKS_ENV, OKS_AGENCY, "overklagande_studiestod", n, 2,
                   SYSTEM.format(agency=OKS_AGENCY).replace("handläggare", "föredragande"), user, meta)
    row["id"] = "%s-%s" % (OKS_ENV, rec.get("diarienummer") or n)
    return row


_CHANGED = {"bifall", "delvis_bifall", "aterforvisning"}


def oks_reward(meta, answer_text):
    """0.7 for the outcome (0.25 if a different outcome on the same side of
    'changed / not changed'), 0.3 for the provisions (F1 against those the board
    cites under "Författningsbestämmelser" and in its assessment)."""
    o = meta["oracle"]
    ans = extract_json(answer_text)
    if not ans or ans.get("utfall") not in OKS_UTFALL:
        return 0.0
    if ans["utfall"] == o["utfall"]:
        score = 0.7
    elif ans["utfall"] in _CHANGED and o["utfall"] in _CHANGED:
        score = 0.25
    else:
        return 0.0
    pred = set()
    for r in ans.get("lagrum") or []:
        pred |= refs(str(r))
    truth = set(o["lagrum"])
    return round(score + 0.3 * f1(pred, truth), 4)


def oks_oracle(meta):
    def back(r):
        kap, par, law = r.split(":", 2)
        name = next((k for k, v in LAW_ALIASES.items() if v == law), None)
        law = name or ("lagen (%s)" % law if re.fullmatch(r"\d{4}:\d+", law) else law)
        return ("%s kap. " % kap if kap else "") + "%s § %s" % (par, law)
    return json.dumps({"utfall": meta["oracle"]["utfall"], "lagrum": [back(r) for r in meta["oracle"]["lagrum"]],
                       "motivering": meta["beslutsmening"]}, ensure_ascii=False)


# ------------------------------------------------------- Skolinspektionen

SI_ENV = "skolinspektionen_tillsyn"
SI_AGENCY = "Statens skolinspektion"
SI_INGRIPANDE = ["inget_ingripande", "avstaende_fran_ingripande", "anmarkning", "forelaggande",
                 "forelaggande_med_vite"]
_SI_MAP = {"ingen_brist": "inget_ingripande", "avstående_från_ingripande": "avstaende_fran_ingripande",
           "anmarkning": "anmarkning", "foreläggande": "forelaggande",
           "foreläggande_med_vite": "forelaggande_med_vite"}
_FACTS = "Omständigheter som ligger till grund för bedömningen"
_NO_BRIST = re.compile(
    r"framkommer inte annat än att|framkommer inte att|inte framkommit (?:några )?brist|får anses bedriva|"
    r"inga brister|inte finns (?:någon |några )?brist|"
    r"brist(?:en|erna)?(?: därmed)? (?:är|har) avhjälp|har avhjälpts|"
    r"(?<!inte )(?<!inte fullt ut )uppfyller författningarnas krav", re.I)
_BRIST = re.compile(
    r"uppfyller inte|inte (?:fullt ut )?uppfyller|inte motsvarar|motsvarar inte|inte lever upp|lever inte upp|"
    r"inte (?:fullt ut )?har följt|har inte (?:fullt ut )?följt|alltjämt finns brist|detta är en brist|"
    r"inte överensstämmer|överensstämmer inte|följer inte|inte följer|"
    r"(?<!inte )finns (?:[a-zåäö]+ ){0,4}brist|(?:utgör|är|förelåg) en brist|inte alltid uppfyller", re.I)


_SI_LEAD = re.compile(r"^(?:(?:Motivering av beslutet|Föreläggande(?: vid vite)?|Anmärkning|Avstående från ingripande|"
                      r"Områden? utan konstaterade? brist(?:er)?|Områden? med konstaterade? brist(?:er)?)\s+)+", re.I)


_SI_PERSONAL = re.compile(r"ledningskretsens insikt|Lämplighetsprövningen har avsett|ägar- och ledningsprövning", re.I)


def _brist(assessment):
    """True / False from the opening of an area's assessment, None if unclear."""
    head = re.sub(r"\s+", " ", assessment[:900])
    a, b = _BRIST.search(head), _NO_BRIST.search(head)
    if a and (not b or a.start() < b.start()):
        return True
    if b:
        return False
    return None


_SI_MAP["uppfoljning_avslutad"] = "inget_ingripande"


def si_parse(rec):
    label = _SI_MAP.get(rec.get("outcome"))
    t = rec["text"].replace("\x0c", "\n")
    if not label or _FACTS not in t:
        return None
    if _SI_PERSONAL.search(t):
        return None        # vetting of owners' and managers' personal suitability: about individuals
    head = re.search(r"\nÄrendet[ \t]*\n(.*?)\n\s*Beslut[ \t]*\n", "\n" + t, re.S)
    om = re.search(r"\nOm ärendet[ \t]*\n(.*?)\nMotivering av beslutet", t, re.S)
    parts = t.split(_FACTS)
    areas = []
    for i in range(1, len(parts)):
        block = [l.strip() for l in parts[i - 1].rstrip().split("\n\n")[-1].split("\n") if l.strip()]
        name = " ".join(block) if block and len(" ".join(block)) < 200 else (block[-1] if block else "")
        name = _SI_LEAD.sub("", name).strip()       # these headings state the outcome
        seg = parts[i]
        facts = re.split(r"\nRättslig reglering[ \t]*\n|\nSkolinspektionens bedömning[ \t]*\n", seg)[0]
        bed = re.search(r"\nSkolinspektionens bedömning[ \t]*\n(.*?)(?:\nVal av ingripande|\Z)", seg, re.S)
        bed = bed.group(1) if bed else ""
        brist = _brist(bed)
        facts = scrub(facts)
        if len(facts) > 200:
            areas.append({"omrade": name, "omstandigheter": facts, "brist": brist})
    if not areas or any(a["brist"] is None for a in areas):
        return None
    any_brist = any(a["brist"] for a in areas)
    if any_brist != (label != "inget_ingripande"):
        return None                      # rule labels disagree with the decision: drop
    return {"arendet": scrub((head.group(1) if head else "") + "\n" + (om.group(1) if om else "")),
            "omraden": areas, "ingripande": label,
            "lagrum": sorted(r for r in refs(t) if r.split(":")[0] == "26" and r.endswith("2010:800"))}


SI_FORMAT = """Svara med ett JSON-objekt:
{"omraden": [{"omrade": "områdets namn", "brist": true | false}, ...],
 "ingripande": "inget_ingripande" | "avstaende_fran_ingripande" | "anmarkning" | "forelaggande" | "forelaggande_med_vite",
 "lagrum": ["26 kap. 10 § skollagen", ...],
 "motivering": "bedömningen i korthet"}
Ange det mest ingripande beslutet om flera områden har brister."""


def si_task(rec, n, max_chars=24000):
    p = si_parse(rec)
    if not p:
        return None
    body = []
    for a in p["omraden"]:
        body.append("Område: %s\nOmständigheter som ligger till grund för bedömningen:\n%s"
                    % (a["omrade"], a["omstandigheter"]))
    text = "\n\n".join(body)
    if len(text) > max_chars:
        return None
    user = ("Skolinspektionen har genomfört en tillsyn. Underlaget är hämtat ur ett offentliggjort beslut. "
            "Bedöm för varje område om det finns en brist och föreslå ingripande enligt 26 kap. skollagen.\n\n"
            "Verksamhet: %s, %s (%s huvudman)\nÄrendet: %s\n\n%s\n\n%s"
            % (rec.get("school_form") or "", rec.get("decision_type") or "", rec.get("operator_type") or "okänd",
               p["arendet"], text, SI_FORMAT))
    meta = {"oracle": {"ingripande": p["ingripande"],
                       "omraden": [{"omrade": a["omrade"], "brist": a["brist"]} for a in p["omraden"]],
                       "lagrum": p["lagrum"]},
            "diarienummer": rec.get("diarienummer"), "date": rec.get("date"), "url": rec.get("url"),
            "decision_type": rec.get("decision_type"), "doc_id": rec.get("doc_id")}
    row = task_row(SI_ENV, SI_AGENCY, "tillsynsbeslut", n, 2, SYSTEM.format(agency=SI_AGENCY), user, meta)
    # an original decision and its follow-up share diarienummer, so the document id is part of the key
    row["id"] = "%s-%s" % (SI_ENV, re.sub(r"\W+", "-", "%s-%s" % (rec.get("diarienummer") or n, rec.get("doc_id") or n)))
    return row


def si_reward(meta, answer_text):
    """0.4 deficiency per area (accuracy, matched in order), 0.5 intervention
    (0.2 if one step off on the severity scale), 0.1 intervention paragraph."""
    o = meta["oracle"]
    ans = extract_json(answer_text)
    if not ans or ans.get("ingripande") not in SI_INGRIPANDE:
        return 0.0
    truth = [a["brist"] for a in o["omraden"]]
    pred = [a.get("brist") for a in (ans.get("omraden") or []) if isinstance(a, dict)]
    acc = sum(1 for x, y in zip(pred, truth) if x is y) / len(truth) if len(pred) == len(truth) else 0.0
    d = abs(SI_INGRIPANDE.index(ans["ingripande"]) - SI_INGRIPANDE.index(o["ingripande"]))
    score = 0.4 * acc + (0.5 if d == 0 else 0.2 if d == 1 else 0.0)
    pred_refs = set()
    for r in ans.get("lagrum") or []:
        pred_refs |= refs(str(r))
    want = {"anmarkning": "26:11:2010:800", "forelaggande": "26:10:2010:800",
            "forelaggande_med_vite": "26:27:2010:800", "avstaende_fran_ingripande": "26:12:2010:800"}.get(o["ingripande"])
    score += 0.1 * ((want in pred_refs) if want else True)
    return round(score, 4)


def si_oracle(meta):
    o = meta["oracle"]
    par = {"anmarkning": "11", "forelaggande": "10", "forelaggande_med_vite": "27",
           "avstaende_fran_ingripande": "12"}.get(o["ingripande"])
    return json.dumps({"omraden": o["omraden"], "ingripande": o["ingripande"],
                       "lagrum": ["26 kap. %s § skollagen" % par] if par else [], "motivering": ""},
                      ensure_ascii=False)
