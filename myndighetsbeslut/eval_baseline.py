#!/usr/bin/env python3
"""Run a local model (served by Ollama) on the test split and score it.

    python3 eval_baseline.py --model gpt-oss:20b [--max-per-env 60] [--think low]
    python3 eval_baseline.py --report            # table over all finished runs

Nothing leaves the machine: requests go to localhost only. The run is
resumable (results/<model>.jsonl) and stops if free memory drops below 15 %.
"""
import argparse
import collections
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

from myndighetsbeslut import csn, forvaltning, published as P

HERE = os.path.dirname(os.path.abspath(__file__))
ENVS = {
    csn.ENV: (csn.reward, lambda o: o["utfall"], "utfall"),
    forvaltning.ENV: (forvaltning.reward, lambda o: o["atgard"], "atgard"),
    P.OKS_ENV: (P.oks_reward, lambda o: o["utfall"], "utfall"),
    P.SI_ENV: (P.si_reward, lambda o: o["ingripande"], "ingripande"),
}
MIN_FREE_PCT = 15      # macOS "system-wide memory free percentage"; stop below this


def available_pct():
    out = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout
    m = re.search(r"free percentage:\s*(\d+)%", out)
    return int(m.group(1)) if m else 100


def chat(model, messages, think, num_ctx, num_predict, host):
    body = {"model": model, "messages": messages, "stream": False, "keep_alive": "3m",
            "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": num_predict}}
    if think:
        body["think"] = think if think in ("low", "medium", "high") else (think == "true")
    req = urllib.request.Request(host + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.loads(r.read())
    return d["message"].get("content", ""), d.get("eval_count", 0), d.get("prompt_eval_count", 0)


def test_tasks(max_per_env):
    for env in ENVS:
        rows = []
        with open(os.path.join(HERE, "data", "tasks", env + ".jsonl"), encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                if r["split"] == "test":
                    rows.append(r)
        step = max(1, len(rows) // max_per_env) if max_per_env else 1
        for r in rows[::step][:max_per_env or None]:
            yield r


def run(args):
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    path = os.path.join(HERE, "results", re.sub(r"[^\w.-]+", "_", args.model) + ".jsonl")
    done = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            done = {json.loads(l)["id"] for l in fh if l.strip()}
    with open(path, "a", encoding="utf-8") as out:
        for n, task in enumerate(test_tasks(args.max_per_env)):
            if task["id"] in done:
                continue
            free = available_pct()
            if free < MIN_FREE_PCT:
                print("stopping: only %d %% memory free" % free)
                return 2
            reward_fn, label, key = ENVS[task["environment"]]
            t0 = time.time()
            try:
                text, n_out, n_in = chat(args.model, task["responses_create_params"]["input"], args.think,
                                         args.num_ctx, args.num_predict, args.host)
                err = None
            except Exception as e:                       # keep going; a failed call scores 0
                text, n_out, n_in, err = "", 0, 0, repr(e)[:200]
            meta = task["verifier_metadata"]
            ans = P.extract_json(text) or {}
            rec = {"id": task["id"], "environment": task["environment"], "difficulty": task["difficulty"],
                   "reward": reward_fn(meta, text), "truth": label(meta["oracle"]), "pred": ans.get(key),
                   "valid_json": bool(ans), "seconds": round(time.time() - t0, 1), "tokens_out": n_out,
                   "tokens_in": n_in, "with_rules": meta.get("with_rules"), "error": err, "answer": text[:3000]}
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            print("%3d %-26s %.2f  %s -> %s  (%.0fs, %d%% mem free)" % (
                n, task["environment"], rec["reward"], rec["truth"], rec["pred"], rec["seconds"], free), flush=True)
    return 0


def macro_f1(pairs):
    labels = {t for t, _ in pairs}
    fs = []
    for l in labels:
        tp = sum(1 for t, p in pairs if t == l and p == l)
        fp = sum(1 for t, p in pairs if t != l and p == l)
        fn = sum(1 for t, p in pairs if t == l and p != l)
        fs.append(2 * tp / (2 * tp + fp + fn) if tp else 0.0)
    return sum(fs) / len(fs) if fs else 0.0


def report():
    stats = json.load(open(os.path.join(HERE, "data", "tasks", "stats.json"), encoding="utf-8"))
    lines = ["| Model | Environment | n | Mean reward | Constant answer | Decision accuracy | Macro-F1 | Valid JSON |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    extra = []
    for path in sorted(glob.glob(os.path.join(HERE, "results", "*.jsonl"))):
        model = os.path.basename(path)[:-6]
        by = collections.defaultdict(list)
        with open(path, encoding="utf-8") as fh:
            for l in fh:
                r = json.loads(l)
                by[r["environment"]].append(r)
        for env in ENVS:
            rs = by.get(env)
            if not rs:
                continue
            pairs = [(r["truth"], r["pred"]) for r in rs]
            lines.append("| %s | %s | %d | %.3f | %.3f | %.0f %% | %.2f | %.0f %% |" % (
                model, env, len(rs), sum(r["reward"] for r in rs) / len(rs),
                stats[env]["constant_answer_baseline"]["mean_reward"],
                100 * sum(t == p for t, p in pairs) / len(rs), macro_f1(pairs),
                100 * sum(r["valid_json"] for r in rs) / len(rs)))
        c = by.get(csn.ENV, [])
        if c:
            must = [r for r in c if r["truth"] in ("avslag", "komplettering")]
            unsafe = sum(r["pred"] in ("bifall", "delvis_bifall") for r in must)
            full = sum(r["reward"] == 1.0 for r in c)
            w = [r["reward"] for r in c if r["with_rules"]]
            wo = [r["reward"] for r in c if r["with_rules"] is False]
            extra.append("- %s, CSN: %d of %d cases that must be rejected or completed were approved; "
                         "%d of %d fully correct; mean reward %.3f with the rule excerpt (n=%d), %.3f without (n=%d)."
                         % (model, unsafe, len(must), full, len(c), sum(w) / max(len(w), 1), len(w),
                            sum(wo) / max(len(wo), 1), len(wo)))
            for d in (1, 2, 3):
                rs = [r["reward"] for r in c if r["difficulty"] == d]
                if rs:
                    extra.append("  - difficulty %d: %.3f (n=%d)" % (d, sum(rs) / len(rs), len(rs)))
        secs = [r["seconds"] for rs in by.values() for r in rs]
        errs = sum(bool(r["error"]) for rs in by.values() for r in rs)
        extra.append("- %s: %d tasks, %.0f min total, %d failed calls." % (model, len(secs), sum(secs) / 60, errs))
    text = "\n".join(lines + [""] + extra)
    print(text)
    return text


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--max-per-env", type=int, default=0, help="0 = whole test split")
    ap.add_argument("--think", default="", help="low|medium|high|true|false (model dependent)")
    ap.add_argument("--num-ctx", type=int, default=16384)
    ap.add_argument("--num-predict", type=int, default=2500)
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        report()
    elif a.model:
        sys.exit(run(a))
    else:
        ap.error("--model or --report")
