# myndighetskort

Turns every regleringsbrev in [PierreMesure/oppna-statsliggaren](https://huggingface.co/datasets/PierreMesure/oppna-statsliggaren)
into a structured **agency card**: goals, assignments with deadlines and partners, appropriations with
conditions, loans and payment plan. The cards are the task source for agency-specific agents and RL
environments. Parsing is deterministic (regex, no model calls).

## Run

```bash
mkdir -p data && curl -L -o data/letters.parquet https://huggingface.co/datasets/PierreMesure/oppna-statsliggaren/resolve/main/letters.parquet
python3 build_cards.py      # ~80 s, reads the parquet in batches of 200 rows
python3 build_diffs.py      # amendment diffs
python3 validate.py         # coverage table per year
python3 -m pytest -q tests
```

Needs `pyarrow`. `data/` and `out/` are git-ignored.

## Output

| Path | Content |
| --- | --- |
| `out/cards/{year}.jsonl.gz` | One card per letter, originals and amendments (16 333 letters, 2003–2026) |
| `out/agencies/2026/{slug}.json` | Latest version of each agency's current-year card, pretty-printed (188 agencies) |
| `out/amendments/{year}.jsonl.gz` | What each ändringsbeslut changed against the previous version |
| `out/index.csv` | One row per letter: counts, totals, warnings |
| `out/coverage.txt` | Output of `validate.py` |

In this repository the 188 current cards are checked in under `cards_2026/` and the coverage table as `coverage.txt`.

## Card schema (v1)

- Metadata: `id`, `original_rbid`, `type`, `category`, `date`, `year`, `name`, `slug`, `department`,
  `expenditure_areas`, `registry_numbers`, `pdf`, `riksdag_refs`, `layout`.
- `mission`: text under VERKSAMHET before the first subsection.
- `goals`, `org`, `assignments`: lists of items. Each item has
  `item_id`, `title`, `group` (parent headings), `text`, `deadlines` (dates preceded by "senast"),
  `dates` (all dates), `forecast_dates`, `recipients`, `partners` (other agencies named),
  `channels` (årsredovisning, Hermes, delredovisning ...), `task_types`, `sub_questions` (bullets),
  `anslag_refs`, `expenditure_area_refs`, `sfs_refs`, `ongoing`.
- `appropriations`: `code`, `name`, `kind`, `expenditure_area`, `older_budget_year`,
  `disposals[{by, amount_tkr, items[{ap, name, amount_tkr}], sum_ok}]`,
  `conditions[{ap, name, text}]`, `caps` ("högst N kronor" limits).
- `financial_terms` (credit, carry-over, withdrawal per item), `loans`, `payment_plan`,
  `finance_tables`, `fee_tables`, `exceptions_text`.
- `summary` and `warnings`.

## Coverage

From `validate.py` on the full dataset:

- 2006–2026 agency letters: 93–99 % yield goal or assignment items, 94–99 % yield appropriations,
  84–96 % yield a payment plan. Every disposal whose item sum could be checked matches its total.
- 2003–2005 use an older layout: goals and appropriations parse, but there are no item-level sums
  or payment plans, and items are split on every bold line, so they are much finer grained.
- Amendments: 8 824 diffs; for 1 001 (11 %) no change was detected, which means the change sits
  in a part that is not compared (attachments, free text outside the parsed sections) or was lost.

## Known limits

- `partners` is a name match against agencies in the dataset plus a short alias list; an agency
  that is only mentioned counts as a partner. `task_types` is keyword based.
- An item's title is whatever heading or bold line precedes it; 1–2 % of recent items are untitled.
- The source Markdown is lossy. Attachments (Word, Excel, PDF) are not parsed.
- The letters carry the yearly steering only. Standing duties are in each agency's instruktion (SFS).
