#!/usr/bin/env python3
"""Convert the decision tasks to typed questions in the shape Sven / Kev trains on.

    python3 export_sven.py [--max-state-chars 20000]

One record = one case file (`state`) and one or more typed questions about it:

    {"state": {...},
     "questions": {"utfall": {"type": "choice", "instructions": "...", "criteria": {key: description}, "label": key},
                   "brist":  {"type": "noul",   "instructions": "...", "criteria": {"true": "...", "false": "..."}, "label": true}},
     "_meta": {"id", "group_id", "source", "env", "agency", "split", "lang", "state_chars", ...}}

Only `choice` and `noul` (yes/no) questions are produced. Amounts in kronor, free-text provisions and written
reasons from the generative version have no typed equivalent and are left out.

Writes sven_dataset/{train,dev,heldout}.jsonl, one file per split with all environments, and stats.json.
A group (one decision or one generated case) never crosses splits.
"""
import argparse
import collections
import hashlib
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "sven_dataset")
SPLIT = {"train": "train", "val": "dev", "test": "heldout"}


def choice(instructions, criteria, label):
    assert label in criteria, (label, list(criteria))
    return {"type": "choice", "instructions": instructions, "criteria": criteria, "label": label}


def noul(instructions, label, yes=None, no=None):
    q = {"type": "noul", "instructions": instructions, "label": bool(label)}
    if yes or no:
        q["criteria"] = {"true": yes, "false": no}
    return q


def rng_for(task_id):
    return random.Random(int(hashlib.sha1(task_id.encode()).hexdigest()[:12], 16))


# ----------------------------------------------------------------------- CSN

CSN_UTFALL = {
    "bifall": "Fullt studiemedel för hela den sökta tiden",
    "delvis_bifall": "Studiemedel beviljas, men för färre veckor eller med lägre belopp än fullt",
    "avslag": "Ansökan avslås",
    "komplettering": "Ansökan är ofullständig och måste kompletteras innan den kan prövas",
}
# reasons that are established whenever the application is complete
CSN_AVSLAG = {
    "utbildning_ej_studiemedelsberattigande": "Saknar utbildningen rätt till studiemedel?",
    "alder_over_60": "Är sökanden för gammal för att få studiemedel?",
    "medborgarskap_uppehallstillstand": "Hindrar sökandens medborgarskap eller uppehållstillstånd rätten till studiemedel?",
    "under_halvtid": "Är studietakten för låg för studiemedel?",
    "kortare_an_tre_veckor": "Är studietiden för kort för studiemedel?",
    "studieresultat": "Är de tidigare studieresultaten otillräckliga för nya studiemedel?",
    "veckor_slut": "Har sökanden redan använt alla veckor med studiemedel på utbildningsnivån?",
    "annan_forman": "Har sökanden under perioden en annan förmån som hindrar studiemedel?",
    "obetalt_arsbelopp": "Hindrar obetalda årsbelopp från tidigare år nya studiemedel?",
    "obetalt_aterkrav": "Hindrar obetalda återkrav nya studiemedel?",
}
# reasons that are only established when no ground for rejection applies
CSN_BEGRANSNING = {
    "retroaktiv_grans": "Kom ansökan in så sent att en del av studietiden inte kan ge studiemedel?",
    "veckograns": "Räcker de återstående veckorna med studiemedel inte till hela den sökta tiden?",
    "lanetrappa": "Begränsar sökandens ålder antalet veckor med studielån?",
    "inkomstminskning": "Ska studiemedlen minskas på grund av inkomst över fribeloppet?",
}


def csn_record(task, n_negative=3):
    m = task["verifier_metadata"]
    o, case = m["oracle"], m["case"]
    state = {"arende": "Ansökan om studiemedel hos Centrala studiestödsnämnden (CSN)", "akt": case}
    if m.get("with_rules"):
        user = task["responses_create_params"]["input"][1]["content"]
        state["regelunderlag"] = user.split("Regelunderlag", 1)[1].split("\n\nSvara med ett JSON-objekt")[0] \
            .split(":", 1)[1].strip() if "Regelunderlag" in user else None
    q = {"utfall": choice("Hur ska ansökan om studiemedel avgöras?", CSN_UTFALL, o["utfall"])}
    q["behover_kompletteras"] = noul("Behöver ansökan kompletteras innan den kan prövas?", o["utfall"] == "komplettering")
    rng = rng_for(task["id"])
    if o["utfall"] != "komplettering":
        pool = dict(CSN_AVSLAG)
        if o["utfall"] in ("bifall", "delvis_bifall"):
            pool.update(CSN_BEGRANSNING)
        true = [k for k in pool if k in o["skal"]]
        false = [k for k in pool if k not in o["skal"]]
        for k in true + rng.sample(false, min(n_negative, len(false))):
            q[k] = noul(pool[k], k in o["skal"])
        q["gar_sokanden_emot"] = noul(
            "Går beslutet helt eller delvis sökanden emot, så att det ska innehålla en överklagandehänvisning?",
            o["overklagande"] is not None)
    return state, q, {"scenario": m["scenario"], "with_rules": bool(m.get("with_rules"))}


# ------------------------------------------------------------ förvaltningslagen

FL_ATGARD = {
    "rattidsprovning": ("Vad ska myndigheten göra med överklagandet?", {
        "avvisa": "Överklagandet har kommit in för sent och ska avvisas",
        "andra_beslutet_och_overlamna": "Beslutet är uppenbart felaktigt och ska ändras; överklagandet och det nya beslutet överlämnas till överinstansen",
        "overlamna": "Överklagandet ska överlämnas till överinstansen"}),
    "jav": ("Vad ska handläggaren göra?", {
        "anmal_jav": "Anmäla omständigheten som jäv och inte delta i handläggningen",
        "fortsatt_handlaggning": "Fortsätta handläggningen; omständigheten utgör inte jäv"}),
    "kommunicering": ("Vad ska myndigheten göra innan ärendet avgörs?", {
        "kommunicera": "Underrätta parten om materialet och ge tillfälle att yttra sig",
        "fatta_beslut": "Fatta beslut direkt; kommunicering är uppenbart obehövlig"}),
}


def fl_record(task):
    m = task["verifier_metadata"]
    typ, o, case = m["case_type"], m["oracle"], m["case"]
    state = {"arende": {"rattidsprovning": "Ett beslut har överklagats", "jav": "Fråga om jäv",
                        "kommunicering": "Fråga om kommunicering före beslut"}[typ], "akt": case}
    ins, crit = FL_ATGARD[typ]
    q = {"atgard": choice(ins, crit, o["atgard"])}
    if typ == "rattidsprovning":
        in_time = case["overklagandet_inkom_till_myndigheten"] <= o["sista_dag"]
        q["inkom_i_ratt_tid_till_myndigheten"] = noul(
            "Kom överklagandet in till beslutsmyndigheten inom tre veckor från det att klaganden fick del av beslutet?", in_time)
        q["ska_avvisas"] = noul("Ska överklagandet avvisas som för sent inkommet?", o["atgard"] == "avvisa")
    elif typ == "jav":
        q["jav"] = noul("Är handläggaren jävig?", o["atgard"] == "anmal_jav")
    else:
        q["kommunicering_kravs"] = noul("Måste parten få yttra sig över materialet innan beslutet fattas?",
                                         o["atgard"] == "kommunicera")
    return state, q, {"case_type": typ}


# ------------------------------------------------------------------------ ÖKS

OKS_UTFALL = {
    "avslag": "Överklagandet avslås; CSN:s beslut står fast",
    "bifall": "Beslutet ändras till klagandens fördel, eller nämnden avgör en fråga till klagandens fördel och CSN prövar resten",
    "delvis_bifall": "Beslutet ändras delvis till klagandens fördel och överklagandet avslås i övrigt",
    "aterforvisning": "Ärendet återförvisas till CSN för ny prövning",
    "avvisning": "Överklagandet tas inte upp till prövning",
}


def oks_record(task):
    m = task["verifier_metadata"]
    o = m["oracle"]
    state = {"arende": "Överklagande av CSN:s beslut till Överklagandenämnden för studiestöd", **m["input"]}
    q = {"utfall": choice("Hur avgör nämnden överklagandet?", OKS_UTFALL, o["utfall"]),
         "andras_till_klagandens_fordel": noul(
             "Ändras CSN:s beslut helt eller delvis till klagandens fördel?",
             o["utfall"] in ("bifall", "delvis_bifall", "aterforvisning"))}
    return state, q, {"date": m.get("date"), "area": m.get("area"), "diarienummer": m.get("diarienummer")}


# ------------------------------------------------------------------------ ÖNH

ONH_UTFALL = {
    "avslag": "Överklagandet avslås; lärosätets beslut står fast",
    "bifall": "Beslutet ändras eller undanröjs till klagandens fördel",
    "delvis_bifall": "Beslutet ändras delvis till klagandens fördel och överklagandet avslås i övrigt",
    "aterforvisning": "Ärendet lämnas tillbaka till lärosätet för ny prövning",
    "avvisning": "Överklagandet tas inte upp till prövning, till exempel för att beslutet inte får överklagas",
}


def onh_record(task):
    m = task["verifier_metadata"]
    o = m["oracle"]
    state = {"arende": "Överklagande av ett lärosätes beslut till Överklagandenämnden för högskolan", **m["input"]}
    q = {"utfall": choice("Hur avgör nämnden överklagandet?", ONH_UTFALL, o["utfall"]),
         "andras_till_klagandens_fordel": noul(
             "Ändras eller undanröjs lärosätets beslut helt eller delvis till klagandens fördel?",
             o["utfall"] in ("bifall", "delvis_bifall", "aterforvisning")),
         "provas_i_sak": noul("Tar nämnden upp överklagandet till prövning i sak?", o["utfall"] != "avvisning")}
    return state, q, {"date": m.get("date"), "area": m.get("area"), "diarienummer": m.get("diarienummer")}


# ------------------------------------------------------------------------- JO

JO_UTFALL = {
    "ingen_kritik": "JO finner inte skäl att kritisera myndigheten",
    "uttalande": "JO uttalar sig i en rättslig fråga utan att rikta kritik",
    "kritik": "Myndigheten kritiseras för sin handläggning",
    "allvarlig_kritik": "Myndigheten får allvarlig kritik",
}


def jo_record(task):
    m = task["verifier_metadata"]
    o = m["oracle"]
    state = {"arende": "JO:s granskning av en myndighets handläggning", **m["input"]}
    krit = o["utfall"] in ("kritik", "allvarlig_kritik")
    q = {"utfall": choice("Hur bedömer JO myndighetens handläggning?", JO_UTFALL, o["utfall"]),
         "kritik": noul("Kritiserar JO myndigheten?", krit)}
    if krit:
        q["allvarlig"] = noul("Är kritiken allvarlig?", o["utfall"] == "allvarlig_kritik")
    return state, q, {"date": m.get("date"), "area": m.get("area"), "diarienummer": m.get("diarienummer")}


# ---------------------------------------------------------- Skolinspektionen

SI_INGRIPANDE = {
    "inget_ingripande": "Inga brister konstateras, eller tidigare brister är avhjälpta; ärendet avslutas",
    "avstaende_fran_ingripande": "En brist finns men Skolinspektionen avstår från att ingripa",
    "anmarkning": "Mindre allvarlig brist; huvudmannen tilldelas en anmärkning",
    "forelaggande": "Huvudmannen föreläggs att avhjälpa bristerna",
    "forelaggande_med_vite": "Huvudmannen föreläggs vid vite att avhjälpa bristerna",
}


def si_records(task, max_chars):
    """One record per assessed area (yes/no: deficiency) and, if it fits, one for the whole decision."""
    m = task["verifier_metadata"]
    o, inp = m["oracle"], m["input"]
    extra = {"date": m.get("date"), "decision_type": m.get("decision_type"), "diarienummer": m.get("diarienummer")}
    out = []
    for i, (a, lab) in enumerate(zip(inp["omraden"], o["omraden"])):
        state = {"arende": "Skolinspektionens tillsyn", "verksamhet": inp["verksamhet"],
                 "omrade": a["omrade"], "omstandigheter": a["omstandigheter"]}
        q = {"brist": noul("Finns det en brist inom området, det vill säga uppfyller verksamheten inte författningarnas krav?",
                           lab["brist"], "Verksamheten uppfyller inte författningarnas krav inom området",
                           "Det framkommer inte annat än att kraven är uppfyllda, eller en tidigare brist är avhjälpt")}
        out.append(("omrade%d" % i, state, q, dict(extra, level="omrade")))
    state = {"arende": "Skolinspektionens tillsyn", "verksamhet": inp["verksamhet"], "beskrivning": inp["arendet"],
             "omraden": inp["omraden"]}
    if len(json.dumps(state, ensure_ascii=False)) <= max_chars:
        q = {"ingripande": choice("Vilket är det mest ingripande beslut som Skolinspektionen fattar?",
                                  SI_INGRIPANDE, o["ingripande"]),
             "nagon_brist": noul("Konstaterar Skolinspektionen någon brist i verksamheten?",
                                 any(x["brist"] for x in o["omraden"]))}
        out.append(("beslut", state, q, dict(extra, level="beslut")))
    return out


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-state-chars", type=int, default=20000,
                    help="records with a longer state are dropped (about 6-7k tokens)")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    files = {s: open(os.path.join(OUT, s + ".jsonl"), "w", encoding="utf-8") for s in SPLIT.values()}
    stats = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    dropped = collections.Counter()

    def emit(task, env, suffix, state, questions, extra):
        chars = len(json.dumps(state, ensure_ascii=False))
        if chars > args.max_state_chars:
            dropped[env] += 1
            return
        split = SPLIT[task["split"]]
        meta = {"id": task["id"] + (":" + suffix if suffix else ""), "group_id": task["id"],
                "source": "myndighetsbeslut", "env": env, "agency": task["agency"], "split": split,
                "lang": "sv", "difficulty": task["difficulty"], "state_chars": chars}
        meta.update(extra)
        files[split].write(json.dumps({"state": state, "questions": questions, "_meta": meta}, ensure_ascii=False) + "\n")
        stats[env][split]["records"] += 1
        for name, q in questions.items():
            stats[env][split]["questions"] += 1
            stats[env][split]["q:%s" % q["type"]] += 1
            stats[env]["labels"]["%s=%s" % (name if q["type"] == "choice" else "noul", q["label"])] += 1

    def tasks(env):
        path = os.path.join(HERE, "data", "tasks", env + ".jsonl")
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                yield json.loads(line)

    for t in tasks("csn_studiemedel"):
        emit(t, "csn_studiemedel", "", *csn_record(t))
    for t in tasks("forvaltning_nasta_steg"):
        emit(t, "forvaltning_nasta_steg", "", *fl_record(t))
    for t in tasks("oks_overklagande"):
        emit(t, "oks_overklagande", "", *oks_record(t))
    for t in tasks("onh_overklagande"):
        emit(t, "onh_overklagande", "", *onh_record(t))
    for t in tasks("jo_tillsyn"):
        emit(t, "jo_tillsyn", "", *jo_record(t))
    for t in tasks("skolinspektionen_tillsyn"):
        for suffix, state, q, extra in si_records(t, args.max_state_chars):
            emit(t, "skolinspektionen_tillsyn", suffix, state, q, extra)
    for f in files.values():
        f.close()
    summary = {env: {k: dict(v) for k, v in d.items()} for env, d in stats.items()}
    summary["_dropped_over_length"] = dict(dropped)
    summary["_max_state_chars"] = args.max_state_chars
    with open(os.path.join(OUT, "stats.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    for env, d in summary.items():
        if env.startswith("_"):
            print(env, d)
            continue
        print(env, {s: d[s] for s in d if s != "labels"})
        print("   ", dict(sorted(d["labels"].items(), key=lambda x: -x[1])[:14]))


if __name__ == "__main__":
    main()
