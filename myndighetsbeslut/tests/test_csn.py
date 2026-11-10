import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from myndighetsbeslut import csn, forvaltning, lag  # noqa: E402


def base(**kw):
    a = {"kalenderar": 2026, "fodelsear": 2000, "medborgarskap": "svensk_medborgare", "niva": "eftergymnasial",
         "utbildning_ger_ratt_till_studiemedel": True, "har_trearig_gymnasieutbildning": True,
         "studietakt_procent": 100, "studiestart": "2026-08-31", "studieveckor": 20,
         "ansokan_inkom": "2026-08-03", "soker_lan": True, "inkomst_kalenderhalvaret": 0,
         "barn_med_vardnad_under_18": 0, "veckor_anvanda_heltidsekvivalenter": 0,
         "veckor_med_lan_tidigare": 0, "tidigare_studieresultat": None, "annan_forman_under_perioden": None,
         "obetalt_arsbelopp_fran_tidigare_ar": False, "aterkrav_kvar_antal_kalenderhalvar": 0,
         "saknas_i_ansokan": []}
    a.update(kw)
    return a


def test_published_amounts_2026():
    # CSN's published weekly amounts and fribelopp for 2026 (sources/csn_rules_2026.json)
    assert csn.veckobelopp(100, 2026) == {"totalt": 3398, "bidrag": 1030, "lan": 2368}
    assert csn.veckobelopp(75, 2026) == {"totalt": 2551, "bidrag": 775, "lan": 1776}
    assert csn.veckobelopp(50, 2026) == {"totalt": 1699, "bidrag": 509, "lan": 1190}
    assert csn.fribelopp(100, 20, 2026) == 114676
    assert csn.fribelopp(100, 10, 2026) == 172041
    assert csn.fribelopp(50, 8, 2026) == 206400


def test_fribelopp_table_matches_source():
    src = json.load(open(os.path.join(os.path.dirname(csn.__file__), "..", "sources", "csn_rules_2026.json"),
                         encoding="utf-8"))
    table = next(r["value"] for r in src["rules"] if r["key"] == "fribelopp_table_2026")
    n = 0
    for weeks, row in table.items():
        for pace, amount in row.items():
            assert csn.fribelopp(int(pace), int(weeks), 2026) == amount, (weeks, pace)
            n += 1
    assert n >= 60


def test_full_approval():
    o = csn.decide(base())
    assert o["utfall"] == "bifall" and o["bidrag_kr"] == 20600 and o["lan_kr"] == 47360
    assert o["overklagande"] is None


def test_grant_only_and_children():
    o = csn.decide(base(soker_lan=False, barn_med_vardnad_under_18=2))
    assert o["lan_kr"] == 0 and o["tillaggsbidrag_kr"] == (195 + 124) * 20


def test_income_reduction():
    o = csn.decide(base(inkomst_kalenderhalvaret=114676 + 10000))
    assert o["utfall"] == "delvis_bifall" and o["skal"] == ["inkomstminskning"]
    assert o["bidrag_kr"] + o["lan_kr"] == 3398 * 20 - 6100
    assert o["overklagande"]["instans"] == "Överklagandenämnden för studiestöd"
    assert csn.decide(base(inkomst_kalenderhalvaret=114676))["utfall"] == "bifall"


def test_rejections():
    assert csn.decide(base(fodelsear=1965))["skal"] == ["alder_over_60"]
    assert csn.decide(base(fodelsear=1966))["utfall"] == "bifall"
    assert csn.decide(base(studietakt_procent=25))["skal"] == ["under_halvtid"]
    assert csn.decide(base(aterkrav_kvar_antal_kalenderhalvar=1))["utfall"] == "bifall"
    assert csn.decide(base(aterkrav_kvar_antal_kalenderhalvar=2))["skal"] == ["obetalt_aterkrav"]
    res = {"veckor_med_studiemedel_fore_perioden": 40, "poang_atagande": 60, "poang_klarade": 37.5}
    assert csn.decide(base(tidigare_studieresultat=dict(res)))["utfall"] == "bifall"
    assert csn.decide(base(tidigare_studieresultat=dict(res, poang_klarade=36)))["skal"] == ["studieresultat"]
    res80 = dict(res, veckor_med_studiemedel_fore_perioden=80, poang_klarade=44)
    assert csn.decide(base(tidigare_studieresultat=res80))["utfall"] == "avslag"


def test_limits():
    o = csn.decide(base(ansokan_inkom="2026-10-21"))       # week of 19 Oct; earliest 21 Sep
    assert o["skal"] == ["retroaktiv_grans"] and o["veckor_beviljade"] == 17
    o = csn.decide(base(veckor_anvanda_heltidsekvivalenter=230))
    assert o["skal"] == ["veckograns"] and o["veckor_beviljade"] == 10
    o = csn.decide(base(fodelsear=1970, veckor_med_lan_tidigare=115))     # 56 years: 120 loan weeks
    assert o["skal"] == ["lanetrappa"] and o["lan_kr"] == 2368 * 5 and o["bidrag_kr"] == 1030 * 20


def test_oracle_and_verifier():
    for d in (1, 2, 3):
        for s in range(150):
            m = csn.make_task(s, d)["verifier_metadata"]
            assert csn.reward(m, csn.oracle(m)) == 1.0
            assert csn.reward(m, "") == 0.0
            if m["oracle"]["utfall"] in ("avslag", "komplettering"):
                unsafe = json.dumps({"utfall": "bifall", "skal": [], "veckor_beviljade": 20})
                assert csn.reward(m, unsafe) == 0.0


def test_invented_citation_scores_zero():
    assert lag.score_refs(["3 kap. 99 § studiestödslagen"], ["3 kap. 5 § studiestödslagen"]) == 0.0
    assert lag.score_refs(["3 kap. 5 § första stycket studiestödslagen"], ["3 kap. 5 § studiestödslagen"]) == 1.0


def test_forvaltning():
    import datetime as dt
    assert forvaltning.last_day(dt.date(2026, 9, 5)) == dt.date(2026, 9, 28)   # 26 Sep is a Saturday
    for d in (1, 2, 3):
        for s in range(100):
            m = forvaltning.make_task(s, d)["verifier_metadata"]
            assert forvaltning.reward(m, forvaltning.oracle(m)) == 1.0
