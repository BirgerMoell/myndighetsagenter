"""Shared formats for decision tasks.

A task row (JSONL):
    {"id", "environment", "agency", "case_type", "difficulty", "split",
     "responses_create_params": {"input": [system, user]},
     "verifier_metadata": {...}}        # hidden from the model

The model answers with one JSON object. Fields common to all environments:
    utfall      the decision or next step (vocabulary per environment)
    lagrum      list of statute references, e.g. "3 kap. 18 § studiestödslagen"
    motivering  the reasons, in Swedish, addressed to the party
"""
import json
import re

SYSTEM = ("Du är handläggare på {agency}. Du bereder ett beslutsförslag som en ansvarig "
          "beslutsfattare granskar innan det expedieras. Använd bara uppgifterna i akten och "
          "gällande författningar. Hitta inte på uppgifter eller lagrum. "
          "Svara endast med ett JSON-objekt enligt det angivna formatet.")


def extract_json(text):
    """Last valid JSON object in an answer (also inside ```json blocks)."""
    if not text:
        return None
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    elif "<think>" in text:
        return None
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    for c in blocks[::-1] + [text]:
        c = c.strip()
        try:
            v = json.loads(c)
            if isinstance(v, dict):
                return v
        except Exception:
            pass
        starts = [m.start() for m in re.finditer(r"\{", c)]
        ends = [m.start() + 1 for m in re.finditer(r"\}", c)][::-1]
        for s in starts[:20]:
            for e in ends[:20]:
                if e <= s:
                    continue
                try:
                    v = json.loads(c[s:e])
                    if isinstance(v, dict):
                        return v
                except Exception:
                    continue
    return None


def f1(pred, truth):
    pred, truth = set(pred or []), set(truth or [])
    if not pred and not truth:
        return 1.0
    tp = len(pred & truth)
    if not tp:
        return 0.0
    p, r = tp / len(pred), tp / len(truth)
    return 2 * p * r / (p + r)


def task_row(env, agency, case_type, n, difficulty, system, user, meta):
    return {
        "id": "%s-d%d-%05d" % (env, difficulty, n),
        "environment": env,
        "agency": agency,
        "case_type": case_type,
        "difficulty": difficulty,
        "responses_create_params": {"input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user}]},
        "verifier_metadata": meta,
    }
