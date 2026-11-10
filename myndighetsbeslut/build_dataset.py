#!/usr/bin/env python3
"""Write the decision tasks to data/tasks/*.jsonl and data/tasks/stats.json.

    python3 build_dataset.py [--n 400]

Synthetic environments are split by seed (10 % test, 10 % val). Tasks from
published decisions are split by decision date, newest last, so that the test
set is the most recent 15 % (val: the 10 % before that).
"""
import argparse
import collections
import json
import os

from myndighetsbeslut import csn, forvaltning, published as P

HERE = os.path.dirname(os.path.abspath(__file__))


def seed_split(seed):
    return {0: "test", 1: "val"}.get(seed % 10, "train")


def date_split(rows):
    rows.sort(key=lambda r: (r["verifier_metadata"].get("date") or "", r["id"]))
    n = len(rows)
    for i, r in enumerate(rows):
        r["split"] = "test" if i >= n * 0.85 else "val" if i >= n * 0.75 else "train"
    return rows


def read(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def constant_baseline(rows, reward, answers):
    """Best mean reward on the test split of any single constant answer."""
    test = [r for r in rows if r["split"] == "test"] or rows
    best = (0.0, None)
    for a in answers:
        s = sum(reward(r["verifier_metadata"], json.dumps(a, ensure_ascii=False)) for r in test) / len(test)
        if s > best[0]:
            best = (round(s, 3), a.get("utfall") or a.get("ingripande") or a.get("atgard"))
    return {"mean_reward": best[0], "answer": best[1], "n_test": len(test)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=400, help="synthetic tasks per difficulty level")
    args = ap.parse_args()
    out = os.path.join(HERE, "data", "tasks")
    os.makedirs(out, exist_ok=True)
    stats = {}

    def write(env, rows, label, reward, oracle, constants):
        with open(os.path.join(out, env + ".jsonl"), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        orc = [reward(r["verifier_metadata"], oracle(r["verifier_metadata"])) for r in rows]
        stats[env] = {
            "n": len(rows),
            "splits": dict(collections.Counter(r["split"] for r in rows)),
            "labels": dict(collections.Counter(label(r) for r in rows).most_common()),
            "oracle_mean_reward": round(sum(orc) / max(len(orc), 1), 4),
            "constant_answer_baseline": constant_baseline(rows, reward, constants) if rows else None,
        }

    rows = []
    for d in (1, 2, 3):
        for s in range(args.n):
            # every fourth task has no rule excerpt; hashed so that all splits get both kinds
            t = csn.make_task(s, d, with_rules=((s * 2654435761 >> 7) % 4 != 3))
            t["split"] = seed_split(s)
            rows.append(t)
    write(csn.ENV, rows, lambda r: r["verifier_metadata"]["oracle"]["utfall"], csn.reward, csn.oracle,
          [{"utfall": u, "skal": [], "veckor_beviljade": 0, "bidrag_kr": 0, "lan_kr": 0} for u in csn.UTFALL])

    rows = []
    for d in (1, 2, 3):
        for s in range(args.n // 2):
            t = forvaltning.make_task(s, d)
            t["split"] = seed_split(s)
            rows.append(t)
    write(forvaltning.ENV, rows, lambda r: r["verifier_metadata"]["oracle"]["atgard"], forvaltning.reward,
          forvaltning.oracle, [{"atgard": a} for v in forvaltning.ATGARDER.values() for a in v])

    rows = [t for t in (P.oks_task(r, i) for i, r in enumerate(read(os.path.join(HERE, "data", "oks_decisions.jsonl")))) if t]
    write(P.OKS_ENV, date_split(rows), lambda r: r["verifier_metadata"]["oracle"]["utfall"], P.oks_reward,
          P.oks_oracle, [{"utfall": u, "lagrum": []} for u in P.OKS_UTFALL])

    src = read(os.path.join(HERE, "data", "onh_decisions.jsonl"))
    rows = [t for t in (P.onh_task(r, i) for i, r in enumerate(src)) if t]
    write(P.ONH_ENV, date_split(rows), lambda r: r["verifier_metadata"]["oracle"]["utfall"], P.onh_reward,
          P.onh_oracle, [{"utfall": u, "lagrum": []} for u in P.ONH_UTFALL])
    stats[P.ONH_ENV]["source"] = {"collected": len(src), "usable": len(rows)}

    src = read(os.path.join(HERE, "data", "jo_decisions.jsonl"))
    rows = [t for t in (P.jo_task(r, i) for i, r in enumerate(src)) if t]
    write(P.JO_ENV, date_split(rows), lambda r: r["verifier_metadata"]["oracle"]["utfall"], P.jo_reward,
          P.jo_oracle, [{"utfall": u, "lagrum": []} for u in P.JO_UTFALL])
    stats[P.JO_ENV]["source"] = {"collected": len(src), "usable": len(rows)}

    src = read(os.path.join(HERE, "data", "skolinspektionen_decisions.jsonl"))
    rows = [t for t in (P.si_task(r, i) for i, r in enumerate(src)) if t]
    stats_src = {"collected": len(src), "usable": len(rows)}
    write(P.SI_ENV, date_split(rows), lambda r: r["verifier_metadata"]["oracle"]["ingripande"], P.si_reward,
          P.si_oracle, [{"ingripande": u, "omraden": []} for u in P.SI_INGRIPANDE])
    stats[P.SI_ENV]["source"] = stats_src

    with open(os.path.join(out, "stats.json"), "w", encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, indent=1)
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
