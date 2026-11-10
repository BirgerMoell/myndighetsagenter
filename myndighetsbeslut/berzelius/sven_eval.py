#!/usr/bin/env python3
"""Score a Kev-family checkpoint (e.g. birgermoell/sven-0.6b) on Sven-format records.

Run inside the Kev environment (https://github.com/jaredpalmer/kev, `uv sync`):

    uv run python sven_eval.py --run birgermoell/sven-0.6b --data heldout.jsonl --out eval.json

Per environment and question it reports accuracy, the majority-class accuracy to beat, macro-F1,
expected calibration error, mean negative log-likelihood and, for yes/no questions, AUROC.
Records whose state is longer than --max_state tokens are counted as rejected, not silently dropped.
"""
import argparse
import collections
import json
import math
import random
import time

import numpy as np

from kev.checkpoint import Checkpoint, LoadOptions
from kev.data import augment, load_records, materialize
from kev.device import select
from kev.model import training_context
from kev.metrics import ece


def auroc(scores, labels):
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks, i = [0.0] * len(scores), 0
    while i < len(order):                       # average ranks over ties
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    rp = sum(r for r, y in zip(ranks, labels) if y)
    return (rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def macro_f1(pred, truth):
    fs = []
    for c in set(truth):
        tp = sum(1 for p, t in zip(pred, truth) if p == c and t == c)
        fp = sum(1 for p, t in zip(pred, truth) if p == c and t != c)
        fn = sum(1 for p, t in zip(pred, truth) if p != c and t == c)
        fs.append(2 * tp / (2 * tp + fp + fn) if tp else 0.0)
    return sum(fs) / len(fs)


def summarise(rows):
    truth = [r["y"] for r in rows]
    pred = [r["pred"] for r in rows]
    conf = [r["conf"] for r in rows]
    ok = [int(p == t) for p, t in zip(pred, truth)]
    out = {"n": len(rows), "acc": float(np.mean(ok)),
           "majority_acc": collections.Counter(truth).most_common(1)[0][1] / len(truth),
           "macro_f1": macro_f1(pred, truth), "ece": ece(conf, ok),
           "nll": float(np.mean([r["nll"] for r in rows])), "mean_conf": float(np.mean(conf))}
    if all(r["qtype"] == "noul" for r in rows):
        out["auroc"] = auroc([r["p_true"] for r in rows], truth)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="checkpoint directory or hub id")
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0, help="evaluate only the first N records per environment")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max_state", type=int, default=7552,
                    help="state tokens admitted per record (kev.evaluate's own default is 384, which rejects most case files)")
    a = ap.parse_args()
    ctx = training_context(a.max_state)

    records = load_records(a.data, source="myndighetsbeslut")
    seen = collections.Counter()
    keep = []
    for r in records:
        env = r["_meta"].get("env", "custom")
        seen[env] += 1
        if a.limit and seen[env] > a.limit:
            continue
        for qid, q in r["questions"].items():
            q["src"] = "%s/%s" % (env, qid)          # group results by environment and question
        keep.append(r)

    opts = LoadOptions.from_env()
    dev = select(a.device) if opts.dtype is None else select(a.device, dtype=opts.dtype)
    tok, model = Checkpoint(a.run).load(dev, opts)
    rng = random.Random(a.seed)
    by, rejected = collections.defaultdict(list), collections.Counter()
    t0 = time.time()
    for n, r in enumerate(keep):
        env = r["_meta"].get("env", "custom")
        try:
            req = augment(r, rng, p_none=0, p_none_distract=0, p_distract=0)
            rec = materialize(req)
            ps = model.probs(model.encode(tok, rec, strict=True, max_state=ctx["max_state"], max_branch=ctx["max_branch"]))
        except ValueError as exc:                    # state or branch over the checkpoint's context
            rejected[env] += 1
            if rejected[env] <= 2:
                print("rejected %s: %s" % (r["_meta"].get("id"), str(exc)[:160]), flush=True)
            continue
        for q, p, src_q in zip(rec["questions"], ps, req["questions"].values()):
            p = p.numpy()
            y = int(q["label"])
            if q["qtype"] == "choice":          # option order may be shuffled per record: compare option names, not positions
                keys = list(src_q["criteria"])
                assert keys[y] == src_q["label"], (keys, y, src_q["label"])
                y_name, pred_name = src_q["label"], keys[int(p.argmax())]
            else:
                y_name, pred_name = y, int(p.argmax())
            row = {"y": y_name, "pred": pred_name, "conf": float(p.max()), "nll": -math.log(max(float(p[y]), 1e-9)),
                   "qtype": q["qtype"], "id": r["_meta"].get("id")}
            if q["qtype"] == "noul":
                row["p_true"] = float(p[1]) if len(p) == 2 else float(p.max())
            by[q["src"]].append(row)
        if (n + 1) % 100 == 0:
            print("%d/%d records, %.0fs" % (n + 1, len(keep), time.time() - t0), flush=True)

    res = {"run": a.run, "data": a.data, "n_records": len(keep), "rejected": dict(rejected), "max_state": a.max_state,
           "seconds": round(time.time() - t0, 1), "questions": {}, "environments": {}}
    envs = collections.defaultdict(list)
    for src, rows in sorted(by.items()):
        res["questions"][src] = summarise(rows)
        envs[src.split("/")[0]].extend(rows)
    for env, rows in envs.items():
        s = {"n": len(rows), "acc": float(np.mean([int(r["pred"] == r["y"]) for r in rows]))}
        s["majority_acc"] = float(np.average([res["questions"][q]["majority_acc"] for q in res["questions"] if q.startswith(env + "/")],
                                             weights=[res["questions"][q]["n"] for q in res["questions"] if q.startswith(env + "/")]))
        res["environments"][env] = s
    print(json.dumps({k: res[k] for k in ("rejected", "seconds", "environments")}, indent=1))
    print("%-58s %5s %6s %6s %6s %6s %6s" % ("question", "n", "acc", "major", "mF1", "ece", "auroc"))
    for src, s in res["questions"].items():
        print("%-58s %5d %6.3f %6.3f %6.3f %6.3f %6s" % (src, s["n"], s["acc"], s["majority_acc"], s["macro_f1"], s["ece"],
                                                           "%.3f" % s["auroc"] if s.get("auroc") is not None else "-"))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
