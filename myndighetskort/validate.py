#!/usr/bin/env python3
"""Coverage report for the parsed cards: python3 validate.py [out]"""
import collections, glob, gzip, json, os, sys
out = sys.argv[1] if len(sys.argv) > 1 else "out"
rows = []
for path in sorted(glob.glob(os.path.join(out, "cards", "*.jsonl.gz"))):
    y = os.path.basename(path)[:4]
    c = collections.Counter()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            k = json.loads(line)
            if k["category"] != "Myndighet":
                c["anslag_letters"] += 1
                c["anslag_with_appr"] += bool(k["appropriations"])
                continue
            c["n"] += 1
            s = k["summary"]
            c["items"] += bool(s["n_goals"] + s["n_assignments"] + s["n_org"])
            c["untitled"] += sum(1 for it in k["goals"] + k["assignments"] if not it["title"])
            c["all_items"] += s["n_goals"] + s["n_assignments"]
            c["appr"] += bool(k["appropriations"])
            d = [x["sum_ok"] for a in k["appropriations"] for x in a["disposals"]]
            c["sum_checked"] += sum(1 for x in d if x is not None)
            c["sum_ok"] += sum(1 for x in d if x)
            if k["payment_plan"]:
                c["plan"] += 1
            for w in k["warnings"]:
                c["w_" + w] += 1
    rows.append((y, c))
print("year  agency  with_items  untitled/items  with_appr  sums_ok/checked  pay_plan  | anslag letters with_appr | warnings")
for y, c in rows:
    w = {k[2:]: v for k, v in c.items() if k.startswith("w_")}
    print("%s  %5d  %5.0f%%   %5d/%-6d  %5.0f%%   %6d/%-6d  %5.0f%%  | %4d %4.0f%% | %s" % (
        y, c["n"], 100 * c["items"] / max(c["n"], 1), c["untitled"], c["all_items"],
        100 * c["appr"] / max(c["n"], 1), c["sum_ok"], c["sum_checked"],
        100 * c["plan"] / max(c["n"], 1), c["anslag_letters"],
        100 * c["anslag_with_appr"] / max(c["anslag_letters"], 1), w))
