"""CSN: ansökan om studiemedel. Rule engine, case generator, oracle and verifier.

The cases are synthetic. The rules are the real ones, taken from the statute
text in sources/lag (studiestödslagen 1999:1395, studiestödsförordningen
2000:655) and the price base amount in rules/csn.json. Deliberate limits, so
that every generated case has one defensible answer:

- one application = one calendar half-year, studies in Sweden;
- post-secondary (3 kap. 2 § första stycket) or upper-secondary adult
  education (andra stycket); no compulsory-level studies;
- only the general grant level (not the higher grant in 3 kap. 13 §), no
  supplementary loan or extra-cost loan, no studies abroad;
- the discretionary escapes (särskilda / synnerliga skäl) appear only as
  "documented illness" for the study-result test; the generator states it
  explicitly as accepted by the file;
- combinations whose treatment the statute text alone does not settle are not
  generated (income reduction together with child supplement, with a shortened
  period, or with the age-related loan ceiling).
"""
import datetime as dt
import json
import random
from fractions import Fraction
from pathlib import Path

from . import lag
from .schema import SYSTEM, extract_json, f1, task_row

ENV = "csn_studiemedel"
AGENCY = "Centrala studiestödsnämnden"
RULES = json.loads((Path(__file__).resolve().parent / "rules" / "csn.json").read_text(encoding="utf-8"))

# studiestödslagen 3 kap. 11, 12 och 17 §§ (procent av prisbasbeloppet)
PCT = {
    100: {"total": "5.74", "bidrag": "1.74", "fri": "193.71", "fri_steg": "9.69"},
    75: {"total": "4.31", "bidrag": "1.31", "fri": "242.14", "fri_steg": "7.26"},
    50: {"total": "2.87", "bidrag": "0.86", "fri": "290.57", "fri_steg": "4.84"},
}
# studiestödsförordningen 3 kap. 11 a-11 c §§: barn 1, barn 2, barn 3+
TILLAGG = {100: ("0.33", "0.21", "0.11"), 75: ("0.25", "0.16", "0.08"), 50: ("0.16", "0.11", "0.06")}
# studiestödslagen 3 kap. 9 §
LANETRAPPA = {51: 220, 52: 200, 53: 180, 54: 160, 55: 140, 56: 120, 57: 100, 58: 80, 59: 60, 60: 40}
MINSKNING = Fraction(61, 100)          # 3 kap. 18 §
LAGSTA_ANDEL = Fraction(25, 10000)     # 3 kap. 22 §
RETRO_VECKOR = 4                       # 3 kap. 37 § studiestödsförordningen
SSL, SSF, FL = "studiestödslagen", "studiestödsförordningen", "förvaltningslagen"

UTFALL = ["bifall", "delvis_bifall", "avslag", "komplettering"]
SKAL = {
    "utbildning_ej_studiemedelsberattigande": ["3 kap. 2 § " + SSL],
    "alder_over_60": ["3 kap. 3 § " + SSL],
    "medborgarskap_uppehallstillstand": ["3 kap. 4 § " + SSL],
    "under_halvtid": ["3 kap. 5 § " + SSL],
    "kortare_an_tre_veckor": ["3 kap. 5 § " + SSL],
    "studieresultat": ["3 kap. 7 § " + SSL, "3 kap. 5 § " + SSF],
    "veckor_slut": ["3 kap. 8 § " + SSL],
    "annan_forman": ["3 kap. 25 § " + SSL],
    "obetalt_arsbelopp": ["3 kap. 26 § " + SSL],
    "obetalt_aterkrav": ["3 kap. 26 § " + SSL],
    "inkomst_for_hog": ["3 kap. 18 § " + SSL, "3 kap. 22 § " + SSL],
    # begränsande skäl (delvis bifall)
    "retroaktiv_grans": ["3 kap. 37 § " + SSF],
    "veckograns": ["3 kap. 8 § " + SSL],
    "lanetrappa": ["3 kap. 9 § " + SSL],
    "inkomstminskning": ["3 kap. 16 § " + SSL, "3 kap. 17 § " + SSL, "3 kap. 18 § " + SSL,
                         "3 kap. 21 § " + SSL],
    # komplettering
    "ofullstandig_ansokan": ["20 § " + FL],
}
OVERKLAGANDE = {"instans": "Överklagandenämnden för studiestöd", "tid_veckor": 3,
                "ges_in_till": AGENCY,
                "lagrum": ["6 kap. 11 § " + SSL, "43 § " + FL, "44 § " + FL]}


def pbb(year):
    v = RULES["prisbasbelopp"].get(str(year))
    if not v:
        raise ValueError("prisbasbelopp saknas för %s i rules/csn.json" % year)
    return v


def kr(pct, base):
    """Percentage of the price base amount, rounded down to whole kronor."""
    return int(Fraction(pct) * base / 100)


def veckobelopp(takt, year):
    b = pbb(year)
    total, bidrag = kr(PCT[takt]["total"], b), kr(PCT[takt]["bidrag"], b)
    return {"totalt": total, "bidrag": bidrag, "lan": total - bidrag}


def fribelopp(takt, veckor, year):
    """3 kap. 17 §: base share at 20 weeks, +/- a step per week shorter/longer."""
    share = Fraction(PCT[takt]["fri"]) + (20 - veckor) * Fraction(PCT[takt]["fri_steg"])
    return int(share * pbb(year) / 100)


def monday(d):
    return d - dt.timedelta(days=d.weekday())


def decide(a):
    """a: case facts (see generate). Returns the oracle decision."""
    year = a["kalenderar"]
    takt, veckor = a["studietakt_procent"], a["studieveckor"]
    age = year - a["fodelsear"]
    out = {"utfall": None, "skal": [], "veckor_beviljade": 0, "bidrag_kr": 0, "lan_kr": 0,
           "tillaggsbidrag_kr": 0, "totalt_kr": 0, "begar_in": [], "berakning": {}}

    if a["saknas_i_ansokan"]:
        out.update(utfall="komplettering", skal=["ofullstandig_ansokan"],
                   begar_in=list(a["saknas_i_ansokan"]))
        return _finish(out)

    avslag = []
    if not a["utbildning_ger_ratt_till_studiemedel"]:
        avslag.append("utbildning_ej_studiemedelsberattigande")
    if age > 60:
        avslag.append("alder_over_60")
    if a["medborgarskap"] == "tidsbegransat_uppehallstillstand_for_studier":
        avslag.append("medborgarskap_uppehallstillstand")
    if takt < 50:
        avslag.append("under_halvtid")
    if veckor < 3:
        avslag.append("kortare_an_tre_veckor")
    res = a.get("tidigare_studieresultat")
    if res:
        krav = Fraction(625, 1000) if res["veckor_med_studiemedel_fore_perioden"] <= 40 else Fraction(3, 4)
        klarat = Fraction(str(res["poang_klarade"])) / Fraction(str(res["poang_atagande"]))
        out["berakning"]["studieresultat"] = {"krav_procent": float(krav * 100),
                                             "klarat_procent": round(float(klarat * 100), 1)}
        if klarat < krav and not res.get("styrkt_sjukdom_godtagen"):
            avslag.append("studieresultat")
    if a["annan_forman_under_perioden"]:
        avslag.append("annan_forman")
    if a["obetalt_arsbelopp_fran_tidigare_ar"]:
        avslag.append("obetalt_arsbelopp")
    if a["aterkrav_kvar_antal_kalenderhalvar"] > 1:
        avslag.append("obetalt_aterkrav")
    max_fte = 240 if a["niva"] == "eftergymnasial" else (80 if a["har_trearig_gymnasieutbildning"] else 120)
    kvar_fte = max_fte - a["veckor_anvanda_heltidsekvivalenter"]
    if kvar_fte <= 0:
        avslag.append("veckor_slut")
    if avslag:
        out.update(utfall="avslag", skal=avslag)
        return _finish(out)

    skal = []
    start = dt.date.fromisoformat(a["studiestart"])
    tidigast = monday(dt.date.fromisoformat(a["ansokan_inkom"])) - dt.timedelta(weeks=RETRO_VECKOR)
    forlorade = 0
    if start < tidigast:
        forlorade = min(veckor, (tidigast - start).days // 7)
        skal.append("retroaktiv_grans")
    beviljade = veckor - forlorade
    tak = int(Fraction(kvar_fte * 100, takt))
    if beviljade > tak:
        beviljade = tak
        skal.append("veckograns")
    if beviljade <= 0:
        out.update(utfall="avslag", skal=skal)
        return _finish(out)

    v = veckobelopp(takt, year)
    lan_veckor = beviljade if a["soker_lan"] else 0
    if a["soker_lan"] and age >= 51:
        tillatet = max(0, LANETRAPPA[age] - a["veckor_med_lan_tidigare"])
        if tillatet < lan_veckor:
            lan_veckor = tillatet
            skal.append("lanetrappa")
    fullt = v["totalt"] * beviljade
    fri = fribelopp(takt, veckor, year)
    over = max(0, a["inkomst_kalenderhalvaret"] - fri)
    out["berakning"].update({"veckobelopp": v, "fribelopp_kr": fri, "overskjutande_inkomst_kr": over})
    if over:
        kvar = Fraction(fullt) - MINSKNING * over
        if kvar <= 0 or kvar / beviljade <= LAGSTA_ANDEL * pbb(year):
            out.update(utfall="avslag", skal=["inkomst_for_hog"])
            return _finish(out)
        andel = Fraction(v["bidrag"], v["totalt"])          # 3 kap. 21 §
        bidrag = int(kvar * andel)
        lan = int(kvar) - bidrag if a["soker_lan"] else 0
        skal.append("inkomstminskning")
        out["berakning"]["minskning_kr"] = float(MINSKNING * over)
    else:
        bidrag = v["bidrag"] * beviljade
        lan = v["lan"] * lan_veckor
    barn = a["barn_med_vardnad_under_18"]
    tillagg = 0
    for i in range(barn):
        tillagg += kr(TILLAGG[takt][min(i, 2)], pbb(year)) * beviljade
    out.update(utfall="delvis_bifall" if skal else "bifall", skal=skal, veckor_beviljade=beviljade,
               bidrag_kr=bidrag, lan_kr=lan, tillaggsbidrag_kr=tillagg,
               totalt_kr=bidrag + lan + tillagg)
    out["berakning"]["lan_veckor"] = lan_veckor
    return _finish(out)


def _finish(out):
    refs = []
    for s in out["skal"]:
        for r in SKAL[s]:
            if r not in refs:
                refs.append(r)
    if out["utfall"] in ("bifall", "delvis_bifall"):
        for r in ("3 kap. 11 § " + SSL, "3 kap. 12 § " + SSL):
            if r not in refs:
                refs.append(r)
    out["lagrum"] = refs
    adverse = out["utfall"] in ("avslag", "delvis_bifall")
    out["overklagande"] = dict(OVERKLAGANDE) if adverse else None
    return out


# ------------------------------------------------------------------ generator

NAMN = ["Amira Lund", "Jonas Ek", "Sofia Bergström", "Karim Haddad", "Linnea Sjöberg", "Oskar Nyström",
        "Hanna Öberg", "Dragan Petrović", "Maja Holm", "Yusuf Demir", "Ebba Sandberg", "Anton Lindgren"]
UTB = {
    "eftergymnasial": ["Sjuksköterskeprogrammet, Högskolan Väst", "Civilingenjörsprogrammet i datateknik, KTH",
                       "Socionomprogrammet, Umeå universitet", "Fristående kurser i statsvetenskap, Lunds universitet",
                       "Yrkeshögskoleutbildning till elkonstruktör"],
    "gymnasial": ["Kommunal vuxenutbildning på gymnasial nivå, vård- och omsorgsutbildning",
                  "Kommunal vuxenutbildning på gymnasial nivå, matematik 2c och svenska 3",
                  "Folkhögskola, allmän kurs på gymnasial nivå"],
}
EJ_UTB = ["Privat kursverksamhet i webbdesign som inte finns i bilagan till studiestödsförordningen",
          "Studiecirkel i spanska hos ett studieförbund"]
SAKNAS = ["uppgift om beräknad inkomst under kalenderhalvåret", "uppgift om studietakt",
          "uppgift om vilka veckor studierna pågår", "studieförsäkran om vilken utbildning ansökan avser"]


def _base(rng, year):
    niva = rng.choice(["eftergymnasial"] * 3 + ["gymnasial"])
    age = rng.randint(20, 45)
    takt = rng.choice([100, 100, 100, 75, 50])
    veckor = rng.choice([20, 20, 20, 18, 10, 22, 15, 8])
    start = monday(dt.date(year, rng.choice([1, 8, 9]), rng.randint(12, 25)))
    if start.month == 1:
        start = monday(dt.date(year, 1, 19))
    inkom = start - dt.timedelta(days=rng.randint(10, 60))
    return {
        "arendenummer": "CSN-%d-%06d" % (year, rng.randrange(10 ** 6)),
        "sokande": rng.choice(NAMN),
        "fodelsear": year - age,
        "kalenderar": year,
        "medborgarskap": rng.choice(["svensk_medborgare"] * 4 + ["permanent_uppehallstillstand_bosatt_i_sverige"]),
        "utbildning": rng.choice(UTB[niva]),
        "niva": niva,
        "utbildning_ger_ratt_till_studiemedel": True,
        "har_trearig_gymnasieutbildning": rng.random() < 0.6,
        "studietakt_procent": takt,
        "studiestart": start.isoformat(),
        "studieveckor": veckor,
        "ansokan_inkom": inkom.isoformat(),
        "soker_lan": rng.random() < 0.75,
        "inkomst_kalenderhalvaret": rng.choice([0, 0, 20000, 45000, 70000, 90000]),
        "barn_med_vardnad_under_18": 0,
        "veckor_anvanda_heltidsekvivalenter": rng.choice([0, 0, 20, 40, 60]),
        "veckor_med_lan_tidigare": 0,
        "tidigare_studieresultat": None,
        "annan_forman_under_perioden": None,
        "obetalt_arsbelopp_fran_tidigare_ar": False,
        "aterkrav_kvar_antal_kalenderhalvar": 0,
        "saknas_i_ansokan": [],
    }


def _results(rng, a, ok):
    fore = rng.choice([20, 40, 60, 80, 120])
    atag = rng.choice([30, 60])
    krav = 0.625 if fore <= 40 else 0.75
    steps = [x * 1.5 for x in range(0, int(atag / 1.5) + 1)]
    good = [p for p in steps if p / atag >= krav]
    bad = [p for p in steps if p / atag < krav and p >= atag * 0.2]
    a["veckor_anvanda_heltidsekvivalenter"] = fore
    a["tidigare_studieresultat"] = {"veckor_med_studiemedel_fore_perioden": fore, "poang_atagande": atag,
                                    "poang_klarade": rng.choice(good if ok else bad),
                                    "styrkt_sjukdom_godtagen": False}


SCENARIOS = {
    1: ["bifall", "bifall_barn", "komplettering", "alder", "under_halvtid", "utbildning", "resultat_ok"],
    2: ["inkomst", "retro", "resultat_underkant", "resultat_sjukdom", "arsbelopp", "aterkrav", "annan_forman",
        "medborgarskap", "kort_tid", "aterkrav_ett_halvar"],
    3: ["veckograns", "lanetrappa", "inkomst_for_hog", "veckor_slut", "retro_veckograns", "tva_avslagsskal",
        "inkomst_precis_under", "resultat_gransfall"],
}


def generate(seed, difficulty=1, year=None):
    rng = random.Random(seed * 7919 + difficulty)
    year = year or RULES["default_year"]
    a = _base(rng, year)
    sc = rng.choice(SCENARIOS[difficulty])
    a["inkomst_kalenderhalvaret"] = min(a["inkomst_kalenderhalvaret"],
                                        fribelopp(a["studietakt_procent"], a["studieveckor"], year) - 1000)
    if sc == "bifall_barn":
        a["barn_med_vardnad_under_18"] = rng.choice([1, 2, 3])
    elif sc == "komplettering":
        a["saknas_i_ansokan"] = rng.sample(SAKNAS, rng.choice([1, 2]))
    elif sc == "alder":
        a["fodelsear"] = year - rng.choice([61, 62, 64])
    elif sc == "under_halvtid":
        a["studietakt_procent"] = 25
    elif sc == "utbildning":
        a["utbildning"], a["utbildning_ger_ratt_till_studiemedel"] = rng.choice(EJ_UTB), False
    elif sc in ("resultat_ok", "resultat_underkant", "resultat_sjukdom", "resultat_gransfall"):
        a["niva"], a["utbildning"] = "eftergymnasial", rng.choice(UTB["eftergymnasial"])
        _results(rng, a, ok=(sc == "resultat_ok"))
        r = a["tidigare_studieresultat"]
        if sc == "resultat_sjukdom":
            r["styrkt_sjukdom_godtagen"] = True
        if sc == "resultat_gransfall":      # exactly at, or one step under, the requirement
            krav = 0.625 if r["veckor_med_studiemedel_fore_perioden"] <= 40 else 0.75
            exact = r["poang_atagande"] * krav
            r["poang_klarade"] = exact if rng.random() < 0.5 else exact - 1.5
    elif sc == "arsbelopp":
        a["obetalt_arsbelopp_fran_tidigare_ar"] = True
        a["veckor_anvanda_heltidsekvivalenter"] = 60
    elif sc in ("aterkrav", "aterkrav_ett_halvar"):
        a["aterkrav_kvar_antal_kalenderhalvar"] = 1 if sc == "aterkrav_ett_halvar" else rng.choice([2, 3])
        a["veckor_anvanda_heltidsekvivalenter"] = 40
    elif sc == "annan_forman":
        a["annan_forman_under_perioden"] = rng.choice(["aktivitetsstöd", "sjukersättning", "aktivitetsersättning"])
    elif sc == "medborgarskap":
        a["medborgarskap"] = "tidsbegransat_uppehallstillstand_for_studier"
    elif sc == "kort_tid":
        a["studieveckor"] = 2
    elif sc in ("inkomst", "inkomst_for_hog", "inkomst_precis_under"):
        fri = fribelopp(a["studietakt_procent"], a["studieveckor"], year)
        fullt = veckobelopp(a["studietakt_procent"], year)["totalt"] * a["studieveckor"]
        if sc == "inkomst":
            a["inkomst_kalenderhalvaret"] = fri + rng.randrange(2000, int(fullt * 0.9), 500)
        elif sc == "inkomst_for_hog":
            a["inkomst_kalenderhalvaret"] = fri + int(fullt / 0.61) + rng.randrange(1000, 40000, 1000)
        else:
            a["inkomst_kalenderhalvaret"] = fri - rng.choice([0, 1, 250])
    elif sc in ("retro", "retro_veckograns"):
        start = dt.date.fromisoformat(a["studiestart"])
        a["studieveckor"] = max(a["studieveckor"], 15)
        a["ansokan_inkom"] = (start + dt.timedelta(days=rng.randint(40, 90))).isoformat()
        if sc == "retro_veckograns":
            a["niva"], a["utbildning"] = "eftergymnasial", rng.choice(UTB["eftergymnasial"])
            a["studietakt_procent"] = 100
            a["veckor_anvanda_heltidsekvivalenter"] = 240 - rng.randint(2, 5)
    elif sc == "veckograns":
        a["niva"], a["utbildning"] = "eftergymnasial", rng.choice(UTB["eftergymnasial"])
        a["studieveckor"] = 20
        a["veckor_anvanda_heltidsekvivalenter"] = 240 - rng.randint(3, 15)
    elif sc == "veckor_slut":
        a["niva"], a["utbildning"] = "gymnasial", rng.choice(UTB["gymnasial"])
        a["veckor_anvanda_heltidsekvivalenter"] = 80 if a["har_trearig_gymnasieutbildning"] else 120
    elif sc == "lanetrappa":
        age = rng.randint(52, 60)
        a.update(fodelsear=year - age, studietakt_procent=100, soker_lan=True, studieveckor=20,
                 inkomst_kalenderhalvaret=0)
        a["veckor_med_lan_tidigare"] = LANETRAPPA[age] - rng.randint(0, 12)
        a["veckor_anvanda_heltidsekvivalenter"] = min(200, a["veckor_med_lan_tidigare"])
    elif sc == "tva_avslagsskal":
        a["obetalt_arsbelopp_fran_tidigare_ar"] = True
        a["annan_forman_under_perioden"] = "aktivitetsstöd"
    if a["tidigare_studieresultat"]:
        p = a["tidigare_studieresultat"]["poang_klarade"]
        a["tidigare_studieresultat"]["poang_klarade"] = int(p) if float(p).is_integer() else p
    return a, sc


REGELUNDERLAG = """Regelunderlag (utdrag, belopp i procent av prisbasbeloppet, som år {year} är {pbb} kronor):
- Studiemedel per vecka: heltid 5,74 %, minst 75 % 4,31 %, minst 50 % 2,87 %; varav studiebidrag 1,74 %, 1,31 % respektive 0,86 % (3 kap. 11-12 §§ studiestödslagen). Beloppen avrundas nedåt till hela kronor per vecka.
- Minst halvtid och minst tre veckors sammanhängande studier (3 kap. 5 §). Längst till och med det år den studerande fyller 60 (3 kap. 3 §).
- Högst 240 veckor på eftergymnasial nivå; 120 veckor på gymnasial nivå, 80 om den studerande redan har treårig gymnasieutbildning (3 kap. 8 §). Vid deltid motsvarande antal veckor (3 kap. 10 §).
- Studielån från det år den studerande fyller 51: sammanlagt högst 220, 200, 180, 160, 140, 120, 100, 80, 60 respektive 40 veckor vid 51-60 år, tidigare låneveckor inräknade (3 kap. 9 §).
- Fribelopp per kalenderhalvår vid 20 studieveckor: heltid 193,71 %, 75 % 242,14 %, 50 % 290,57 %; för varje vecka kortare eller längre studietid ökas respektive minskas andelen med 9,69, 7,26 respektive 4,84 procentenheter (3 kap. 17 §). Inkomst över fribeloppet minskar studiemedlen med 61 % av det överskjutande (3 kap. 18 §), fördelat på bidrag och lån i samma proportion som vid fullt studiemedel (3 kap. 21 §). Studiemedel lämnas inte om beloppet per vecka är högst 0,25 % (3 kap. 22 §).
- Tilläggsbidrag per vecka och barn: heltid 0,33/0,21/0,11 %, 75 % 0,25/0,16/0,08 %, 50 % 0,16/0,11/0,06 % för första, andra respektive varje ytterligare barn (3 kap. 11 a-11 c §§ studiestödsförordningen).
- Prövning av tidigare studieresultat på högskolenivå: 62,5 % av åtagandet under de första 40 veckorna med studiemedel, därefter 75 % (3 kap. 7 § studiestödslagen, 3 kap. 5 § studiestödsförordningen). Särskilda skäl kan godtas.
- Inte för tid med aktivitetsstöd, sjukersättning eller aktivitetsersättning m.m. (3 kap. 25 §). Tidigare årsbelopp ska vara betalda och högst ett kalenderhalvårs återkrav får återstå (3 kap. 26 §).
- Studiemedel beviljas för högst fyra kalenderveckor före den kalendervecka då ansökan kom in (3 kap. 37 § studiestödsförordningen).
- Ofullständig ansökan: hjälp den enskilde till rätta eller förelägg om komplettering (20 § förvaltningslagen). Beslut om studiemedel överklagas till Överklagandenämnden för studiestöd (6 kap. 11 § studiestödslagen); överklagandet ges in till CSN inom tre veckor från det att parten fick del av beslutet (43-44 §§ förvaltningslagen)."""

FORMAT = """Svara med ett JSON-objekt:
{"utfall": "bifall" | "delvis_bifall" | "avslag" | "komplettering",
 "skal": [koder ur: %s],
 "veckor_beviljade": heltal, "bidrag_kr": heltal, "lan_kr": heltal, "tillaggsbidrag_kr": heltal,
 "begar_in": [vad som ska kompletteras, annars tom lista],
 "lagrum": ["3 kap. 5 § studiestödslagen", ...],
 "overklagande": {"instans": "...", "tid_veckor": heltal} eller null om beslutet inte går sökanden emot,
 "motivering": "skälen för beslutet, skrivna till sökanden"}
"delvis_bifall" används när sökanden får mindre än fullt studiemedel för hela den sökta tiden.""" % ", ".join(SKAL)


def make_task(seed, difficulty=1, with_rules=True, year=None):
    a, sc = generate(seed, difficulty, year)
    facit = decide(a)
    system = SYSTEM.format(agency=AGENCY + " (CSN)")
    parts = ["Ärende: ansökan om studiemedel. Uppgifterna i akten är kontrollerade mot register där det är möjligt.",
             "Akt:\n" + json.dumps(a, ensure_ascii=False, indent=1)]
    if with_rules:
        parts.append(REGELUNDERLAG.format(year=a["kalenderar"], pbb=pbb(a["kalenderar"])))
    parts.append(FORMAT)
    meta = {"case": a, "scenario": sc, "oracle": facit, "with_rules": with_rules}
    return task_row(ENV, AGENCY, "ansokan_studiemedel", seed, difficulty, system, "\n\n".join(parts), meta)


def oracle(meta):
    o = meta["oracle"]
    ans = {k: o[k] for k in ("utfall", "skal", "veckor_beviljade", "bidrag_kr", "lan_kr",
                             "tillaggsbidrag_kr", "begar_in", "lagrum")}
    ans["overklagande"] = ({"instans": o["overklagande"]["instans"], "tid_veckor": 3}
                           if o["overklagande"] else None)
    ans["motivering"] = _motivering(meta["case"], o)
    return json.dumps(ans, ensure_ascii=False)


def _motivering(a, o):
    if o["utfall"] == "komplettering":
        return ("Din ansökan kan inte prövas ännu eftersom följande saknas: %s. Komplettera ansökan, "
                "annars kan den komma att inte tas upp till prövning." % "; ".join(o["begar_in"]))
    if o["utfall"] == "avslag":
        return ("CSN avslår din ansökan om studiemedel. Skäl: %s. Tillämpade bestämmelser: %s."
                % (", ".join(o["skal"]), "; ".join(o["lagrum"])))
    return ("CSN beviljar studiemedel för %d veckor med %d kronor i studiebidrag och %d kronor i studielån%s. %s"
            "Tillämpade bestämmelser: %s."
            % (o["veckor_beviljade"], o["bidrag_kr"], o["lan_kr"],
               (" samt %d kronor i tilläggsbidrag" % o["tillaggsbidrag_kr"]) if o["tillaggsbidrag_kr"] else "",
               ("Begränsning: %s. " % ", ".join(o["skal"])) if o["skal"] else "", "; ".join(o["lagrum"])))


def _int(x):
    try:
        return int(round(float(x)))
    except (TypeError, ValueError):
        return None


def reward(meta, answer_text):
    """0..1. Approving what must be rejected or completed gives 0."""
    o = meta["oracle"]
    ans = extract_json(answer_text)
    if not ans or ans.get("utfall") not in UTFALL:
        return 0.0
    truth, pred = o["utfall"], ans["utfall"]
    positive = ("bifall", "delvis_bifall")
    if truth not in positive and pred in positive:
        return 0.0
    reason = f1(ans.get("skal"), o["skal"])
    if pred != truth:
        return round(0.15 * reason, 4)
    score = 0.35 + 0.15 * reason + 0.10 * lag.score_refs(ans.get("lagrum"), o["lagrum"])
    # amounts
    tol = max(2, o["veckor_beviljade"]) if "inkomstminskning" in o["skal"] else 0
    if truth in positive:
        ok = sum([
            _int(ans.get("veckor_beviljade")) == o["veckor_beviljade"],
            _int(ans.get("bidrag_kr")) is not None and abs(_int(ans.get("bidrag_kr")) - o["bidrag_kr"]) <= tol,
            _int(ans.get("lan_kr")) is not None and abs(_int(ans.get("lan_kr")) - o["lan_kr"]) <= tol,
            (_int(ans.get("tillaggsbidrag_kr")) or 0) == o["tillaggsbidrag_kr"],
        ])
        score += 0.25 * ok / 4
    elif truth == "komplettering":
        want = " ".join(o["begar_in"]).lower()
        got = " ".join(str(x) for x in (ans.get("begar_in") or [])).lower()
        keys = [w for w in ("inkomst", "studietakt", "veckor", "utbildning") if w in want]
        score += 0.25 * (sum(k in got for k in keys) / len(keys) if keys else 1.0)
    else:
        zero = all((_int(ans.get(k)) or 0) == 0 for k in ("veckor_beviljade", "bidrag_kr", "lan_kr"))
        score += 0.25 * zero
    # appeal instruction
    ok_appeal = ans.get("overklagande")
    if o["overklagande"]:
        good = (isinstance(ok_appeal, dict)
                and "överklagandenämnden för studiestöd" in str(ok_appeal.get("instans", "")).lower()
                and _int(ok_appeal.get("tid_veckor")) == 3)
        score += 0.10 * good
    else:
        score += 0.10
    mot = ans.get("motivering")
    score += 0.05 * (isinstance(mot, str) and len(mot.strip()) >= 80)
    return round(min(score, 1.0), 4)
