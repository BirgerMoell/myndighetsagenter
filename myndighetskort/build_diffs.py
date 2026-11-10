#!/usr/bin/env python3
"""Diff each amendment (ändringsbeslut) against the previous version of the
same letter. Needs out/cards from build_cards.py.

    python3 build_diffs.py [out]

Writes out/amendments/{year}.jsonl.gz, one record per amendment:
added / removed / changed goals and assignments (matched on title), and
changed amounts per (anslag, disposer, ap), loans and financial terms, and
which appropriation conditions, fee tables or exceptions changed.
"""
import collections
import glob
import gzip
import hashlib
import json
import os
import re
import sys


def norm(t):
    return re.sub(r"\W+", " ", (t or "").lower()).strip()


def items_of(card):
    out = {}
    for sec in ("goals", "org", "assignments"):
        seen = collections.Counter()
        for it in card[sec]:
            base = (sec, norm(" / ".join(it["group"] + [it["title"] or it["text"][:60]])))
            seen[base] += 1
            key = base + (seen[base],)
            out[key] = {"section": sec, "title": it["title"], "group": it["group"],
                        "hash": hashlib.sha1(norm(it["text"]).encode()).hexdigest()[:12],
                        "deadlines": it["deadlines"], "item_id": it["item_id"]}
    return out


def amounts_of(card):
    out = {}
    for a in card["appropriations"]:
        for d in a["disposals"]:
            base = (a["code"], str(a["older_budget_year"] or ""), d["by"] or "")
            out[base + ("",)] = d["amount_tkr"]
            for i in d["items"]:
                out[base + (i["ap"],)] = i["amount_tkr"]
    for k, v in card["loans"].items():
        out[("lån/kredit", "", k, "")] = v
    for t in card["financial_terms"]:
        for h, v in t.items():
            if h not in ("anslag", "ap"):
                out[("villkor:" + h, "", t["anslag"] or "", t["ap"])] = v
    return out


def conditions_of(card):
    out = {}
    for a in card["appropriations"]:
        for c in a["conditions"]:
            out[(a["code"], str(a["older_budget_year"] or ""), c["ap"] or "")] = \
                hashlib.sha1(norm(c["text"]).encode()).hexdigest()[:12]
    out[("avgifter", "", "")] = hashlib.sha1(
        json.dumps(card["fee_tables"], sort_keys=True).encode()).hexdigest()[:12]
    out[("undantag", "", "")] = hashlib.sha1(norm(card["exceptions_text"]).encode()).hexdigest()[:12]
    return out


def diff(prev, cur):
    a, b = items_of(prev), items_of(cur)
    rec = {"added": [], "removed": [], "changed": [], "amount_changes": []}
    for k in b:
        if k not in a:
            rec["added"].append({x: b[k][x] for x in ("section", "title", "group", "item_id", "deadlines")})
        elif a[k]["hash"] != b[k]["hash"]:
            rec["changed"].append({"section": b[k]["section"], "title": b[k]["title"],
                                   "group": b[k]["group"], "item_id": b[k]["item_id"],
                                   "prev_item_id": a[k]["item_id"],
                                   "deadlines_before": a[k]["deadlines"],
                                   "deadlines_after": b[k]["deadlines"]})
    for k in a:
        if k not in b:
            rec["removed"].append({x: a[k][x] for x in ("section", "title", "group", "item_id")})
    x, y = amounts_of(prev), amounts_of(cur)
    for k in sorted(set(x) | set(y)):
        if x.get(k) != y.get(k):
            rec["amount_changes"].append({"anslag": k[0], "older_budget_year": k[1] or None,
                                          "disposer": k[2], "ap": k[3] or None,
                                          "before_tkr": x.get(k), "after_tkr": y.get(k)})
    p, q = conditions_of(prev), conditions_of(cur)
    rec["condition_changes"] = [{"anslag": k[0], "older_budget_year": k[1] or None, "ap": k[2] or None}
                                for k in sorted(set(p) | set(q)) if p.get(k) != q.get(k)]
    return rec


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "out"
    os.makedirs(os.path.join(out, "amendments"), exist_ok=True)
    stats = collections.Counter()
    for path in sorted(glob.glob(os.path.join(out, "cards", "*.jsonl.gz"))):
        year = os.path.basename(path)[:4]
        groups = collections.defaultdict(list)
        with gzip.open(path, "rt", encoding="utf-8") as fh:   # one year in memory at a time
            for line in fh:
                c = json.loads(line)
                key = c["original_rbid"] if c["original_rbid"] else ("name", c["category"], c["name"])
                groups[key].append(c)
        with gzip.open(os.path.join(out, "amendments", year + ".jsonl.gz"), "wt", encoding="utf-8") as w:
            for key, cards in groups.items():
                cards.sort(key=lambda c: (c["type"] != "Regleringsbrev", c["date"] or "", c["id"]))
                for prev, cur in zip(cards, cards[1:]):
                    rec = diff(prev, cur)
                    rec.update({"id": cur["id"], "prev_id": prev["id"],
                                "original_rbid": cur["original_rbid"], "name": cur["name"],
                                "category": cur["category"], "year": year,
                                "date": cur["date"], "prev_date": prev["date"]})
                    w.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    stats["amendments"] += 1
                    stats["with_item_change"] += bool(rec["added"] or rec["removed"] or rec["changed"])
                    stats["with_amount_change"] += bool(rec["amount_changes"])
                    stats["with_condition_change"] += bool(rec["condition_changes"])
                    stats["no_change_found"] += not (rec["added"] or rec["removed"] or rec["changed"]
                                                     or rec["amount_changes"] or rec["condition_changes"])
    print(dict(stats))


if __name__ == "__main__":
    main()
