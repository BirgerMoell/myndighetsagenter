# myndighetsbeslut

Pilot dataset of **decisions a handläggare makes**, with a verifier for each, for three Swedish agencies.
It complements `../myndighetskort/` (what each agency is steered to do) with what the staff actually decide.
The built tasks are published at https://huggingface.co/datasets/birgermoell/myndighetsbeslut.

Each task is one JSONL row: a Swedish prompt
(`responses_create_params`) and hidden `verifier_metadata`. The model answers with one JSON object and
`reward(meta, answer)` returns 0–1. Every task is framed as a proposal that a responsible decision maker reviews.

## The four environments

| Environment | Agency | Ground truth | What the model decides |
| --- | --- | --- | --- |
| `csn_studiemedel` | CSN | Rule engine over the real statute | Grant, partly grant, reject or request completion; weeks, grant and loan in kronor; reasons; provisions; appeal instruction |
| `forvaltning_nasta_steg` | CSN, Skolinspektionen | Rule engine over förvaltningslagen | Next procedural step: late appeal, conflict of interest, duty to communicate |
| `oks_overklagande` | Överklagandenämnden för studiestöd | Real published decisions | Outcome of an appeal against CSN, and the provisions applied |
| `skolinspektionen_tillsyn` | Skolinspektionen | Real published decisions | Deficiency per assessed area, and the most severe intervention |

Sizes on 2026-11-10 (full detail in `data/tasks/stats.json`, written by `build_dataset.py`):

| Environment | Tasks | Labels | Best constant answer on test |
| --- | ---: | --- | --- |
| `csn_studiemedel` | 1 200 | 619 avslag, 327 bifall, 194 delvis bifall, 60 komplettering | 0.35 |
| `forvaltning_nasta_steg` | 600 | 7 next-step classes | 0.19 |
| `oks_overklagande` | 481 | 302 avslag, 158 bifall, 11 återförvisning, 9 delvis bifall, 1 avvisning | 0.59 ("avslag") |
| `skolinspektionen_tillsyn` | 113 | 71 inget ingripande, 33 föreläggande, 7 anmärkning, 2 avstående | 0.26 |

The oracle answer scores 1.0 on every task. A model has to beat the constant-answer column to show any skill.
ÖKS: 490 of the 498 published decisions (2002–2026) were collected and 481 are used.
Skolinspektionen: 200 decisions from 2024–2026 were collected and 113 are used.

## Run

```bash
python3 -m pytest -q tests
python3 build_dataset.py              # writes data/tasks/*.jsonl and stats.json
python3 collect/collect_oks.py --limit 500             # re-fetch published ÖKS decisions (1 request/s, cached)
python3 collect/collect_skolinspektionen.py --limit 200
```

```python
from myndighetsbeslut import csn
task = csn.make_task(seed=7, difficulty=3)
meta = task["verifier_metadata"]
print(csn.reward(meta, csn.oracle(meta)))   # 1.0
```

## How the ground truth is made

**CSN (synthetic cases, real rules).** The percentages come from studiestödslagen 3 kap. and
studiestödsförordningen 3 kap., read from the statute text in `sources/lag/` (fetched from
data.riksdagen.se). The 2026 price base amount is 59 200 kr. The engine reproduces CSN's published weekly
amounts and all 78 cells of CSN's 2026 fribelopp table (`tests/test_csn.py`, source values in
`sources/csn_rules_2026.json`). Covered: eligible education, age limit, residence status, study pace and
length, prior study results (62.5 % / 75 %), week ceilings, loan ceiling from age 51, income test with the
61 % reduction, child supplement, other benefits, unpaid annual amounts and reclaims, the four-week
retroactivity limit, incomplete applications. Three difficulty levels; every fourth task omits the rule
excerpt so the model must know the rules.

**Förvaltningslagen layer (synthetic).** 44–46 §§ (time limit, dismissal, forwarding), 38 § (changing an
obviously wrong decision), 16–18 §§ (conflict of interest), 25 § (communication). The appeal body is taken per
agency.

**ÖKS (real decisions).** `collect/collect_oks.py` downloads the published guiding decisions from oks.se.
The task input is the subject line and the section "Ärendet" (facts, CSN's decision, the appeal). The outcome
sentence and the board's reasoning are cut out. The label is derived from the operative sentence.

**Skolinspektionen (real decisions).** `collect/collect_skolinspektionen.py` downloads supervision decisions.
The input is the case description and, for each assessed area, the section "Omständigheter som ligger till
grund för bedömningen". Legal framing, assessment and choice of intervention are cut out. A decision is used
only if every area's assessment can be classified and the result agrees with the decision's outcome.

## Verifiers

- `csn.reward`: outcome 0.35, amounts and weeks 0.25, reason codes 0.15, provisions 0.10, appeal instruction
  0.10, reasons present 0.05. A wrong outcome scores at most 0.15. **Approving something that must be rejected
  or completed scores 0.** A reference to a paragraph that does not exist in the statute zeroes the provision part.
- `forvaltning.reward`: correct step 0.6, provisions 0.2, last day for appeal (or reasons) 0.2.
- `published.oks_reward`: outcome 0.7 (0.25 for a different outcome on the right side of changed / not
  changed), provisions 0.3.
- `published.si_reward`: deficiency per area 0.4, intervention 0.5 (0.2 if one step off), paragraph 0.1.

The written reasons are only checked for presence. Judging their quality needs a rubric and human-calibrated
judge.

## Limits you should know about

- **CSN cases are simplified on purpose.** One calendar half-year, studies in Sweden, general grant level
  only; no supplementary or extra-cost loans, no compulsory-level studies, no EU-law residence questions.
  Discretionary grounds (särskilda / synnerliga skäl) appear only as "documented illness, accepted in the
  file". Combinations the statute text alone does not settle are not generated. The engine has not been
  reviewed by a CSN case officer.
- **ÖKS labels are skewed.** 63 % of the labelled appeals are rejected, and more in the recent test split, so
  always answering "avslag" scores 0.59 there. Use balanced sampling or macro-F1 when training and evaluating.
  The decisions are selected guiding cases, not a random sample of appeals. "Bifall" includes decisions where
  the board settles one question in the appellant's favour and leaves the rest to CSN.
- **Skolinspektionen labels are rule-derived** from the decision text, and only decisions with the standard
  section layout are used. 87 of the 200 collected decisions were dropped, and decisions with deficiencies
  were dropped more often than those without, so the label mix is not the real one. A spot check of 18 area
  labels against the hidden assessment text found no errors; that is a small sample.
- **Personal data.** The decisions are public documents. Appellants are anonymised by ÖKS as "NN"; two
  decisions whose fact section names a person are excluded (`OKS_EXCLUDE`), as are Skolinspektionen decisions
  vetting the personal suitability of a school's owners. Signature blocks,
  e-mail addresses, phone and organisation numbers are removed from task inputs, but schools and operators are
  named, and raw files under `data/raw/` are unredacted. Treat `data/` as local research material and review it
  before publishing anything.
- **Real decisions must stay with people.** The tasks train decision support. Nothing here is validated for
  deciding real cases.

## Extending to more agencies

1. Rule-computable benefits: add a module like `csn.py` (statute percentages + generator + verifier).
2. Agencies that publish decisions: add a collector and a parser like those in `published.py`.
3. All agencies: `forvaltning.py` already applies; add the agency and its appeal body to `AGENCIES`.
