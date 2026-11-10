#!/usr/bin/env python3
"""Turn letters.parquet from PierreMesure/oppna-statsliggaren into agency cards.

    python3 build_cards.py --parquet data/letters.parquet --out out

Reads the parquet in small batches (never the whole file in memory) and writes:
    out/cards/{year}.jsonl.gz      one card per letter (originals and amendments)
    out/agencies/{year}/{slug}.json  latest version per agency for --current-year
    out/index.csv                  one row per letter with counts and warnings
"""
import argparse
import collections
import csv
import gzip
import json
import os

import pyarrow.parquet as pq

from myndighetskort import build_gazetteer, parse_letter

META = ["id", "original_rbid", "type", "date", "year", "category", "name",
        "department", "expenditure_areas", "registry_numbers", "pdf"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", default="data/letters.parquet")
    ap.add_argument("--out", default="out")
    ap.add_argument("--current-year", default=None)
    ap.add_argument("--batch-size", type=int, default=200)
    ap.add_argument("--years", nargs="*", default=None, help="only these years")
    args = ap.parse_args()

    pf = pq.ParquetFile(args.parquet)
    names, years = set(), set()
    for b in pf.iter_batches(batch_size=5000, columns=["name", "category", "year"]):
        for r in b.to_pylist():
            years.add(r["year"])
            if r["category"] == "Myndighet":
                names.add(r["name"])
    gaz = build_gazetteer(names)
    current = args.current_year or max(years)

    os.makedirs(os.path.join(args.out, "cards"), exist_ok=True)
    writers, latest = {}, {}
    warn = collections.Counter()
    n = 0
    idx_path = os.path.join(args.out, "index.csv")
    with open(idx_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "original_rbid", "type", "category", "year", "date", "name",
                    "department", "n_goals", "n_org", "n_assignments", "n_deadlines",
                    "n_partners", "n_appropriations", "appropriations_total_tkr",
                    "payment_plan_total_tkr", "md_chars", "warnings"])
        for b in pf.iter_batches(batch_size=args.batch_size, columns=META + ["md"]):
            for row in b.to_pylist():
                if args.years and row["year"] not in args.years:
                    continue
                card = parse_letter(row, gaz)
                y = card["year"]
                if y not in writers:
                    writers[y] = gzip.open(os.path.join(args.out, "cards", "%s.jsonl.gz" % y),
                                           "wt", encoding="utf-8")
                writers[y].write(json.dumps(card, ensure_ascii=False) + "\n")
                s = card["summary"]
                w.writerow([card["id"], card["original_rbid"], card["type"], card["category"],
                            y, card["date"], card["name"], card["department"], s["n_goals"],
                            s["n_org"], s["n_assignments"], s["n_deadlines"], len(s["partners"]),
                            s["n_appropriations"], s["appropriations_total_tkr"],
                            s["payment_plan_total_tkr"], s["md_chars"],
                            ";".join(card["warnings"])])
                for x in card["warnings"]:
                    warn[x] += 1
                if y == current and card["category"] == "Myndighet":
                    key = card["name"]
                    stamp = (card["date"] or "", card["id"])
                    if key not in latest or stamp > latest[key][0]:
                        latest[key] = (stamp, card)
                n += 1
    for fh in writers.values():
        fh.close()

    adir = os.path.join(args.out, "agencies", str(current))
    os.makedirs(adir, exist_ok=True)
    for _, card in latest.values():
        with open(os.path.join(adir, card["slug"] + ".json"), "w", encoding="utf-8") as fh:
            json.dump(card, fh, ensure_ascii=False, indent=1)
    print("letters parsed:", n)
    print("agency cards for %s: %d" % (current, len(latest)))
    print("warnings:", dict(warn))


if __name__ == "__main__":
    main()
