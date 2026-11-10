import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import export_sven as E  # noqa: E402
from myndighetsbeslut import csn, forvaltning, published as P  # noqa: E402
from test_published import OKS, SI  # noqa: E402


def check(questions):
    """The shape Sven / Kev trains on: choice with a criteria dict and a label among its keys, noul with a bool."""
    for q in questions.values():
        assert set(q) <= {"type", "instructions", "criteria", "label"} and q["instructions"]
        if q["type"] == "choice":
            assert isinstance(q["criteria"], dict) and q["label"] in q["criteria"]
        else:
            assert q["type"] == "noul" and isinstance(q["label"], bool)


def test_csn_records():
    for d in (1, 2, 3):
        for s in range(120):
            t = csn.make_task(s, d, with_rules=(s % 2 == 0))
            state, q, _ = E.csn_record(t)
            check(q)
            o = t["verifier_metadata"]["oracle"]
            assert q["utfall"]["label"] == o["utfall"]
            assert ("regelunderlag" in state) == (s % 2 == 0)
            if "regelunderlag" in state:
                assert "5,74" in state["regelunderlag"] and "Svara med" not in state["regelunderlag"]
            for k in o["skal"]:
                if k in q:
                    assert q[k]["label"] is True
            # limiting reasons are only asked when they were actually evaluated
            if o["utfall"] in ("avslag", "komplettering"):
                assert not set(q) & set(E.CSN_BEGRANSNING)
            assert all(q[k]["label"] == (k in o["skal"]) for k in q if k in E.CSN_AVSLAG or k in E.CSN_BEGRANSNING)


def test_forvaltning_records():
    for d in (1, 2, 3):
        for s in range(80):
            t = forvaltning.make_task(s, d)
            _, q, _ = E.fl_record(t)
            check(q)
            if "ska_avvisas" in q and q["ska_avvisas"]["label"]:
                assert q["inkom_i_ratt_tid_till_myndigheten"]["label"] is False


def test_published_records():
    rec = {"text": OKS % "beslutar att återkrav inte ska ske.", "diarienummer": "2099-00001", "date": "2099-01-01",
           "area": "Studiemedel / Återkrav"}
    t = P.oks_task(rec, 0)
    state, q, _ = E.oks_record(t)
    check(q)
    assert q["utfall"]["label"] == "bifall" and q["andras_till_klagandens_fordel"]["label"] is True
    assert "beslutar att" not in json.dumps(state, ensure_ascii=False)

    r = {"text": SI, "outcome": "anmarkning", "diarienummer": "SI 2099:1", "date": "2099-01-01",
         "school_form": "Grundskola", "decision_type": "Planerad tillsyn Skolbeslut", "operator_type": "enskild"}
    recs = E.si_records(P.si_task(r, 0), 20000)
    assert [x[0] for x in recs] == ["omrade0", "omrade1", "beslut"]
    assert [x[2]["brist"]["label"] for x in recs[:2]] == [True, False]
    assert recs[2][2]["ingripande"]["label"] == "anmarkning"
    for _, state, q, _ in recs:
        check(q)
        assert "mindre allvarlig" not in json.dumps(state, ensure_ascii=False)
    assert len(E.si_records(P.si_task(r, 0), 500)) == 2      # decision-level record dropped when too long
