import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from myndighetsbeslut import published as P  # noqa: E402

OKS = """ÖVERKLAGANDENÄMNDEN
FÖR STUDIESTÖD

Dnr: 2099-00001

BESLUT
2099-01-01

Klagande

NN

Saken

Återkrav av studiemedel

Överklagandenämndens beslut
Överklagandenämnden %s
Överklagandenämndens beslut får enligt 6 kap. 11 § tredje stycket studiestödslagen
inte överklagas.
Ärendet
NN beviljades studiemedel för höstterminen. CSN beslutade senare att återkräva en del av
studiemedlen eftersom skolan meddelat att NN avbrutit sina studier. NN överklagade och anförde
att hon varit sjuk under perioden och att hon anmält detta till Försäkringskassan i tid. CSN har
i yttrande vidhållit sitt beslut och hänvisat till att sjukperioden inte har godkänts. NN har
fått tillfälle att yttra sig över CSN:s yttrande. Kontakt: klagande@example.org, 070-123 45 67.
Författningsbestämmelser
Enligt 5 kap. 1 § studiestödslagen (1999:1395) ska studiemedel som lämnats felaktigt krävas tillbaka.
Av 3 kap. 27 § studiestödsförordningen framgår när studiemedel får lämnas för en sjukperiod.
Överklagandenämndens bedömning
Nämnden finner att förutsättningarna i 5 kap. 1 § studiestödslagen är uppfyllda.
Beslutet har fattats av en ordförande och ledamöter.
"""


def rec(sentence, dnr="2099-00001"):
    return {"text": OKS % sentence, "diarienummer": dnr, "date": "2099-01-01", "area": "Studiemedel / Återkrav"}


def test_oks_outcomes():
    cases = {
        "avslår överklagandet.": "avslag",
        "beslutar att återkrav inte ska ske.": "bifall",
        "beslutar att återkrav inte ska ske och avslår överklagandet i övrigt.": "delvis_bifall",
        "avvisar överklagandet.": "avvisning",
        "upphäver CSN:s beslut och återförvisar ärendet till CSN.": "aterforvisning",
    }
    for sentence, want in cases.items():
        assert P.oks_parse(rec(sentence))["utfall"] == want, sentence


def test_oks_task_has_no_label_and_no_contact_details():
    t = P.oks_task(rec("avslår överklagandet."), 0)
    user = t["responses_create_params"]["input"][1]["content"]
    assert "avslår" not in user and "Nämnden finner" not in user
    assert "example.org" not in user and "070-123" not in user
    m = t["verifier_metadata"]
    assert set(m["oracle"]["lagrum"]) == {"5:1:1999:1395", "3:27:2000:655"}
    assert P.oks_reward(m, P.oks_oracle(m)) == 1.0
    assert P.oks_reward(m, json.dumps({"utfall": "bifall", "lagrum": []})) == 0.0
    assert P.oks_reward(m, json.dumps({"utfall": "avslag", "lagrum": []})) == 0.7
    assert P.oks_task(rec("avslår överklagandet.", dnr=next(iter(P.OKS_EXCLUDE))), 0) is None


SI = """Beslut
Huvudman
Exempelskolan AB

Ärendet
Tillsyn av grundskolan Exempelskolan i Exempel kommun.

Beslut
Anmärkning
Skolinspektionen tilldelar med stöd av 26 kap. 11 § skollagen (2010:800) huvudmannen en anmärkning.

Om ärendet
Skolinspektionen har genomfört en tillsyn av Exempelskolan med intervjuer och dokumentgranskning.
Motivering av beslutet

Anmärkning
Trygghet och studiero

Omständigheter som ligger till grund för bedömningen
Av intervjuer med elever och lärare framgår att ordningsregler finns men att de inte följs upp
av personalen, och att eleverna inte har medverkat när reglerna togs fram. Rektorn uppger att
arbetet med ordningsreglerna har prioriterats ned under läsåret på grund av personalbrist.
Rättslig reglering
Enligt 5 kap. 5 § skollagen ska ordningsregler utarbetas under medverkan av eleverna.
Skolinspektionens bedömning
Skolinspektionen bedömer att verksamheten inte uppfyller författningarnas krav i denna del.
Val av ingripande
Bristen bedöms som mindre allvarlig.

Områden utan konstaterade brister
Elevhälsa

Omständigheter som ligger till grund för bedömningen
Av utredningen framgår att eleverna har tillgång till skolsköterska, skolläkare, psykolog och
kurator samt personal med specialpedagogisk kompetens, och att elevhälsan arbetar förebyggande
tillsammans med lärarna enligt en dokumenterad plan som följs upp varje termin av rektorn.
Rättslig reglering
Enligt 2 kap. 25 § skollagen ska det finnas elevhälsa.
Skolinspektionens bedömning
Av ovan redovisade omständigheter framkommer inte annat än att verksamheten uppfyller
författningarnas krav inom detta område.
"""


def test_skolinspektionen_task():
    r = {"text": SI, "outcome": "anmarkning", "diarienummer": "SI 2099:1", "date": "2099-01-01",
         "school_form": "Grundskola", "decision_type": "Planerad tillsyn Skolbeslut", "operator_type": "enskild"}
    t = P.si_task(r, 0)
    o = t["verifier_metadata"]["oracle"]
    assert o["ingripande"] == "anmarkning"
    assert o["omraden"] == [{"omrade": "Trygghet och studiero", "brist": True},
                            {"omrade": "Elevhälsa", "brist": False}]
    user = t["responses_create_params"]["input"][1]["content"].split("Svara med ett JSON")[0]
    assert "tilldelar" not in user and "Skolinspektionen bedömer" not in user and "mindre allvarlig" not in user
    m = t["verifier_metadata"]
    assert P.si_reward(m, P.si_oracle(m)) == 1.0
    wrong = json.dumps({"omraden": [{"brist": False}, {"brist": False}], "ingripande": "inget_ingripande"})
    assert P.si_reward(m, wrong) < 0.35
    # a decision whose per-area labels contradict its outcome is dropped
    assert P.si_task(dict(r, outcome="ingen_brist"), 0) is None
