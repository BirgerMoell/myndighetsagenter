# myndighetsagenter

Building blocks for AI agents tailored to Swedish government agencies: what each agency is told to do,
and reinforcement-learning environments for the decisions its case officers make.

- **`myndighetskort/`** turns every *regleringsbrev* (the government's yearly instruction to an agency)
  into a structured **agency card**: goals, assignments with deadlines and partners, appropriations and
  their conditions. 16 333 letters, 2003–2026, 188 agencies in 2026.
- **`myndighetsbeslut/`** holds four **decision environments** with verifiers for three pilot agencies:
  CSN (student aid), Överklagandenämnden för studiestöd (appeals) and Skolinspektionen (school supervision).
  2 394 tasks.

Dataset on Hugging Face: [birgermoell/myndighetsbeslut](https://huggingface.co/datasets/birgermoell/myndighetsbeslut)

This is a research pilot. Nothing here is validated for deciding real cases; every task is framed as a
proposal that a responsible decision maker reviews.

## How it works

```
regleringsbrev (Öppna Statsliggaren)          statutes (riksdagen.se)      published decisions
            │                                         │                    (oks.se, Skolinspektionen)
            ▼                                         ▼                              │
   myndighetskort: agency card            rule engines + case generators            ▼
   goals · assignments · deadlines        CSN studiemedel · förvaltningslagen   fact section in,
   appropriations · conditions                        │                         real outcome as label
            │                                         └──────────────┬───────────────┘
            ▼                                                        ▼
   who the agent works for,                       task = prompt + hidden verifier metadata
   what it must deliver and when                  reward(metadata, model answer) → 0..1
```

### A task

Every task is one JSON line: a Swedish prompt (system + user message) and metadata that only the verifier sees.

The model sees a case file and answers with one JSON object:

```
Ärende: ansökan om studiemedel.

Akt:
{ "fodelsear": 1991, "kalenderar": 2026, "niva": "eftergymnasial", "studietakt_procent": 100,
  "studiestart": "2026-08-17", "studieveckor": 20, "ansokan_inkom": "2026-07-11",
  "soker_lan": true, "inkomst_kalenderhalvaret": 168676, ... }

Regelunderlag (utdrag) ...

Svara med ett JSON-objekt: {"utfall": ..., "skal": [...], "veckor_beviljade": ..., "bidrag_kr": ...,
 "lan_kr": ..., "lagrum": [...], "overklagande": ..., "motivering": "..."}
```

The verifier holds the answer, computed by a rule engine from the statute:

```json
{"utfall": "delvis_bifall", "skal": ["inkomstminskning"], "veckor_beviljade": 20,
 "bidrag_kr": 10615, "lan_kr": 24405,
 "lagrum": ["3 kap. 16 § studiestödslagen", "3 kap. 17 § studiestödslagen", "3 kap. 18 § studiestödslagen", "3 kap. 21 § studiestödslagen"],
 "overklagande": {"instans": "Överklagandenämnden för studiestöd", "tid_veckor": 3}}
```

Here the applicant earns 54 000 kr above the income ceiling, so the aid is reduced by 61 % of that amount
and the reduction is split between grant and loan in proportion.

### The reward

`reward(meta, answer)` is a plain Python function, no judge model. For the CSN environment:

| Part | Weight |
| --- | ---: |
| Correct outcome | 0.35 |
| Weeks and amounts | 0.25 |
| Reason codes | 0.15 |
| Provisions cited | 0.10 |
| Appeal instruction | 0.10 |
| Reasons written | 0.05 |

Two hard rules: approving a case that must be rejected or completed gives **0** for the whole task, and
citing a paragraph that does not exist in the statute gives 0 for the provisions part.

### The four environments

| Environment | Agency | Tasks | Ground truth | Model decides |
| --- | --- | ---: | --- | --- |
| `csn_studiemedel` | CSN | 1 200 | Rule engine over studiestödslagen and studiestödsförordningen | Grant, partly grant, reject or request completion; weeks and kronor; reasons; provisions |
| `forvaltning_nasta_steg` | CSN, Skolinspektionen | 600 | Rule engine over förvaltningslagen | Next procedural step: late appeal, conflict of interest, duty to communicate |
| `oks_overklagande` | Överklagandenämnden för studiestöd | 481 | Published appeal decisions 2002–2026 | Outcome of the appeal and the provisions applied |
| `skolinspektionen_tillsyn` | Skolinspektionen | 113 | Published supervision decisions 2024–2026 | Deficiency per assessed area and the most severe intervention |

The CSN engine reproduces CSN's published 2026 weekly amounts and all 78 cells of its income-ceiling table
(checked in the tests). For the two real-decision environments the outcome and the authority's own reasoning
are cut out of the prompt, and the label is what the authority decided.

## Baseline

One small local model, Qwen 3.5 4B (Ollama, 4-bit, temperature 0, thinking off), on the full test splits,
2026-11-10. "Constant answer" is the mean reward from always giving the single best fixed answer.

| Environment | n | Mean reward | Constant answer | Decision accuracy | Macro-F1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `csn_studiemedel` | 120 | 0.22 | 0.35 | 29 % | 0.15 |
| `forvaltning_nasta_steg` | 60 | 0.43 | 0.19 | 60 % | 0.47 |
| `oks_overklagande` | 72 | 0.67 | 0.59 | 86 % | 0.67 |
| `skolinspektionen_tillsyn` | 16 | 0.65 | 0.26 | 44 % | 0.33 |

- **CSN is unsolved by this model.** It approved 51 of the 67 applications that must be rejected or
  completed, got 0 of 120 fully right, and scores below the constant answer. Reward falls with difficulty:
  0.39, 0.21, 0.08.
- **ÖKS:** barely above always answering "avslag"; it found 2 of the 8 appeals that were upheld.
- **Skolinspektionen:** it never proposed an injunction (0 of 6); the test split is only 16 decisions.
- All answers were valid JSON. One model, one run, no confidence intervals: read it as a floor, not a ranking.
- In `csn_studiemedel` the test split always includes the rule excerpt; the tasks without it are in train only.

Reproduce: `python3 eval_baseline.py --model qwen3.5:4b --think false`, then `--report`.

## Quick start

Python 3.9+. `myndighetsbeslut` needs only the standard library; `myndighetskort` needs `pyarrow`.
The collectors also need the `pdftotext` command, and `eval_baseline.py` a local Ollama server.

```bash
cd myndighetsbeslut
python3 -m pytest -q tests
python3 build_dataset.py        # synthetic tasks; real-decision tasks if you have run the collectors
```

```python
from myndighetsbeslut import csn
task = csn.make_task(seed=7, difficulty=3)
meta = task["verifier_metadata"]
print(task["responses_create_params"]["input"][1]["content"])   # the prompt
print(csn.reward(meta, csn.oracle(meta)))                       # 1.0
```

With the Hugging Face dataset:

```python
import json
from datasets import load_dataset
from myndighetsbeslut import published

ds = load_dataset("birgermoell/myndighetsbeslut", "oks_overklagande", split="test")
row = ds[0]
answer = my_model(row["messages"])                      # your model
print(published.oks_reward(json.loads(row["verifier_metadata"]), answer))
```

Reward functions: `csn.reward`, `forvaltning.reward`, `published.oks_reward`, `published.si_reward`.

Agency cards:

```bash
cd myndighetskort
mkdir -p data && curl -L -o data/letters.parquet https://huggingface.co/datasets/PierreMesure/oppna-statsliggaren/resolve/main/letters.parquet
python3 build_cards.py && python3 build_diffs.py && python3 validate.py
```

The 188 agency cards for 2026 are included in `myndighetskort/cards_2026/`.

## From cards to more environments

The agency card is meant to be both the agent's context and a task generator. Environment families, ordered
by how verifiable the reward is:

| # | Environment | Reward | Status |
| --- | --- | --- | --- |
| 1 | Extract assignments, deadlines, recipients from a letter | Exact match against the card | Data ready (cards) |
| 2 | Appropriation compliance: may this cost be charged to this item? | Deterministic | Data ready (cards) |
| 3 | Amendment handling: what changed, replan | Diff between letter versions | Data ready (`build_diffs.py`) |
| 4 | Year planning of deliverables | Coverage and date checks | Data ready (cards) |
| 5 | Forecasting appropriation use | Scored against outcome | Needs ESV outcome data |
| 6 | Drafting assignment reports | Coverage by program, language by calibrated judge | Not built |
| 7 | Cross-agency coordination | Joint deliverable, routing | Not built |
| 8 | Casework decisions | Rule engines and published decisions | **Pilot built (`myndighetsbeslut/`)** |

## Limits

- **CSN cases are simplified on purpose**: one calendar half-year, studies in Sweden, general grant level,
  no supplementary loans, no EU-law residence questions. The engine has not been reviewed by a CSN case officer.
- **ÖKS labels are skewed**: 63 % of the labelled appeals are rejected. Always answering "avslag" already
  gives 0.59 on the test split, so report macro-F1 and compare against the constant-answer baseline.
- **Skolinspektionen labels are rule-derived**, and decisions with deficiencies were dropped more often than
  those without, so the label mix is not the real one. The sample (200 decisions) is not random.
- **Written reasons are only checked for presence**, not quality.
- **Agency cards** are parsed with rules from lossy Markdown. Coverage is 93–99 % for 2006–2026 and weaker
  for 2003–2005. Partner agencies and task types are keyword matches.

More detail in [`myndighetsbeslut/README.md`](myndighetsbeslut/README.md) and
[`myndighetskort/README.md`](myndighetskort/README.md).

## Personal data

The real-decision tasks come from public documents published by the authorities. ÖKS anonymises appellants
as "NN". Signature blocks, e-mail addresses, phone and organisation numbers are removed from the prompts.
Decisions that name a private person or an outside expert, and decisions vetting the personal suitability of
a school's owners, are left out. Schools and school operators are named, as in the published decisions.
The collectors' raw downloads are not part of this repository. If you find something that should not be
here, open an issue.

## Sources and licence

- Regleringsbrev: [PierreMesure/oppna-statsliggaren](https://huggingface.co/datasets/PierreMesure/oppna-statsliggaren) (CC0), scraped from Statsliggaren.
- Statutes: data.riksdagen.se. CSN amounts for 2026: csn.se (`myndighetsbeslut/sources/csn_rules_2026.json`).
- Decisions: oks.se and Skolinspektionen's decision search.

Code: MIT. Data: CC BY 4.0. See [`LICENSE`](LICENSE) and [`LICENSE-DATA.md`](LICENSE-DATA.md).
