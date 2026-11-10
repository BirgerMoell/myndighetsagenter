"""Shared procedural layer: what a handläggare at any agency must do next
under förvaltningslagen (2017:900). Synthetic cases, real provisions.

Three case types, each with one defensible answer:
  rattidsprovning  an appeal arrives: dismiss as late (45 §), change the
                   decision (38 §) and forward, or forward it (46 §)
  jav              the handler has, or lacks, a disqualifying tie (16-18 §§)
  kommunicering    material from someone else before an adverse decision (25 §)

Dates are drawn from September-November so that the three-week limit never
ends on a public holiday; when it ends on a Saturday or Sunday the limit runs
to the next weekday (lagen 1930:173 om beräkning av lagstadgad tid, 2 §).
"""
import datetime as dt
import json
import random

from . import lag
from .schema import SYSTEM, extract_json, task_row

ENV = "forvaltning_nasta_steg"
FL = "förvaltningslagen"

# agency -> (decision type used in the cases, appeal body)
AGENCIES = {
    "Centrala studiestödsnämnden": ("beslut om avslag på ansökan om studiemedel",
                                    "Överklagandenämnden för studiestöd"),
    "Statens skolinspektion": ("beslut om föreläggande vid vite enligt 26 kap. 27 § skollagen",
                               "allmän förvaltningsdomstol"),
}
ATGARDER = {
    "rattidsprovning": ["avvisa", "andra_beslutet_och_overlamna", "overlamna"],
    "jav": ["anmal_jav", "fortsatt_handlaggning"],
    "kommunicering": ["kommunicera", "fatta_beslut"],
}


def _weekday_on_or_after(d):
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def last_day(received):
    return _weekday_on_or_after(received + dt.timedelta(weeks=3))


def _rattid(rng, agency):
    typ, instans = AGENCIES[agency]
    fick_del = dt.date(2026, 9, 1) + dt.timedelta(days=rng.randrange(60))
    sista = last_day(fick_del)
    variant = rng.choice(["i_tid", "i_tid", "for_sent", "for_sent", "sista_dagen", "fel_hanvisning",
                          "inkom_overinstans", "uppenbart_fel"])
    late = variant in ("for_sent", "fel_hanvisning", "inkom_overinstans")
    inkom = sista + dt.timedelta(days=rng.randint(1, 12)) if late else \
        (sista if variant == "sista_dagen" else fick_del + dt.timedelta(days=rng.randint(2, 18)))
    case = {"myndighet": agency, "overklagat_beslut": typ, "overinstans": instans,
            "klaganden_fick_del_av_beslutet": fick_del.isoformat(),
            "overklagandet_inkom_till_myndigheten": inkom.isoformat(),
            "overklagandehanvisningen_i_beslutet": "korrekt",
            "overklagandet_inkom_forst_till_overinstansen": None,
            "ny_omstandighet": None}
    refs = ["44 § " + FL, "45 § " + FL]
    if variant == "fel_hanvisning":
        case["overklagandehanvisningen_i_beslutet"] = "angav felaktigt fem veckors överklagandetid"
        atgard = "overlamna"
    elif variant == "inkom_overinstans":
        case["overklagandet_inkom_forst_till_overinstansen"] = (sista - dt.timedelta(days=rng.randint(0, 6))).isoformat()
        atgard = "overlamna"
    elif variant == "for_sent":
        atgard = "avvisa"
    elif variant == "uppenbart_fel":
        case["ny_omstandighet"] = ("Klaganden bifogar ett registerutdrag som visar att beslutet bygger på en "
                                   "förväxling av två personer. Beslutet kan rättas snabbt och enkelt och "
                                   "ändringen är inte till nackdel för någon enskild part.")
        atgard = "andra_beslutet_och_overlamna"
        refs += ["38 § " + FL]
    else:
        atgard = "overlamna"
    if atgard != "avvisa":
        refs += ["46 § " + FL]
    return case, {"atgard": atgard, "lagrum": refs, "sista_dag": sista.isoformat(), "variant": variant}


JAV = [
    ("Handläggaren är gift med sökanden.", True),
    ("Handläggarens syster är part i ärendet.", True),
    ("Handläggaren var ombud för parten i just detta ärende innan hon anställdes vid myndigheten.", True),
    ("Handläggaren är delägare i det bolag som beslutet riktar sig mot.", True),
    ("Handläggaren har samma efternamn som parten men de är inte släkt och känner inte varandra.", False),
    ("Handläggaren bor i samma kommun som parten och har ingen annan anknytning till ärendet.", False),
    ("Handläggaren har tidigare handlagt ett annat, avslutat ärende för samma part hos samma myndighet.", False),
]


def _jav(rng, agency):
    text, jav = rng.choice(JAV)
    case = {"myndighet": agency, "arende": AGENCIES[agency][0].replace("beslut om ", "ärende om "),
            "omstandighet": text}
    return case, {"atgard": "anmal_jav" if jav else "fortsatt_handlaggning",
                  "lagrum": ["16 § " + FL, "18 § " + FL] if jav else ["16 § " + FL], "variant": text[:40]}


def _kommunicering(rng, agency):
    variant = rng.choice(["nytt_material_nackdel", "nytt_material_nackdel", "partens_eget_material",
                          "helt_bifall"])
    case = {"myndighet": agency, "arende": AGENCIES[agency][0].replace("beslut om ", "ärende om "),
            "planerat_beslut": "går parten emot", "nytt_material": None, "parten_har_fatt_del_av_materialet": False}
    if variant == "nytt_material_nackdel":
        case["nytt_material"] = rng.choice([
            "Uppgift från en annan myndighet som talar emot partens egna uppgifter",
            "Yttrande från en utomstående som har betydelse för bedömningen"])
        atgard = "kommunicera"
    elif variant == "partens_eget_material":
        case["nytt_material"] = "Endast de handlingar som parten själv har gett in"
        case["parten_har_fatt_del_av_materialet"] = True
        atgard = "fatta_beslut"
    else:
        case["planerat_beslut"] = "bifaller partens ansökan helt"
        case["nytt_material"] = "Registeruppgift som bekräftar partens egna uppgifter"
        atgard = "fatta_beslut"
    return case, {"atgard": atgard, "lagrum": ["25 § " + FL], "variant": variant}


_GEN = {"rattidsprovning": _rattid, "jav": _jav, "kommunicering": _kommunicering}

FORMAT = """Svara med ett JSON-objekt:
{"atgard": ett av %s,
 "sista_dag_for_overklagande": "ÅÅÅÅ-MM-DD" eller null om frågan inte gäller överklagandetid,
 "lagrum": ["25 § förvaltningslagen", ...],
 "motivering": "kort motivering"}"""


def make_task(seed, difficulty=1, agency=None):
    rng = random.Random(seed * 104729 + difficulty)
    agency = agency or rng.choice(sorted(AGENCIES))
    typ = {1: ["jav", "kommunicering"], 2: ["rattidsprovning", "kommunicering", "jav"],
           3: ["rattidsprovning"]}[difficulty]
    typ = rng.choice(typ)
    case, facit = _GEN[typ](rng, agency)
    user = ("Ärendetyp: %s. Vad ska handläggaren göra härnäst enligt förvaltningslagen (2017:900)?\n\nAkt:\n%s\n\n%s"
            % (typ, json.dumps(case, ensure_ascii=False, indent=1), FORMAT % ATGARDER[typ]))
    meta = {"case": case, "case_type": typ, "oracle": facit}
    return task_row(ENV, agency, typ, seed, difficulty, SYSTEM.format(agency=agency), user, meta)


def oracle(meta):
    o = meta["oracle"]
    return json.dumps({"atgard": o["atgard"], "sista_dag_for_overklagande": o.get("sista_dag"),
                       "lagrum": o["lagrum"], "motivering": "Enligt " + ", ".join(o["lagrum"]) + "."},
                      ensure_ascii=False)


def reward(meta, answer_text):
    o = meta["oracle"]
    ans = extract_json(answer_text)
    if not ans or ans.get("atgard") != o["atgard"]:
        return 0.0
    score = 0.6 + 0.2 * lag.score_refs(ans.get("lagrum"), o["lagrum"])
    if "sista_dag" in o:
        score += 0.2 * (ans.get("sista_dag_for_overklagande") == o["sista_dag"])
    else:
        score += 0.2 * (isinstance(ans.get("motivering"), str) and len(ans["motivering"]) > 20)
    return round(score, 4)
