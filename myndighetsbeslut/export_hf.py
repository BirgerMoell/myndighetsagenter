#!/usr/bin/env python3
"""Export data/tasks/*.jsonl to a Hugging Face dataset folder (hf_dataset/).

    python3 export_hf.py

One config per environment, splits train / validation / test. Columns:
    id, environment, agency, case_type, difficulty, label,
    messages            [{role, content}]  the prompt (system + user)
    verifier_metadata   JSON string; parse it and pass it to the environment's reward()

The metadata is a string because its shape differs between environments and
contains nullable and mixed-type fields that do not fit one Arrow schema.
"""
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "hf_dataset")
LABEL = {"csn_studiemedel": "utfall", "forvaltning_nasta_steg": "atgard",
         "oks_overklagande": "utfall", "skolinspektionen_tillsyn": "ingripande"}
SPLIT = {"train": "train", "val": "validation", "test": "test"}


def main():
    if os.path.isdir(os.path.join(OUT, "data")):
        shutil.rmtree(os.path.join(OUT, "data"))
    counts = {}
    for env, key in LABEL.items():
        files = {}
        with open(os.path.join(HERE, "data", "tasks", env + ".jsonl"), encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                split = SPLIT[r["split"]]
                if split not in files:
                    d = os.path.join(OUT, "data", env)
                    os.makedirs(d, exist_ok=True)
                    files[split] = open(os.path.join(d, split + ".jsonl"), "w", encoding="utf-8")
                row = {"id": r["id"], "environment": env, "agency": r["agency"], "case_type": r["case_type"],
                       "difficulty": r["difficulty"], "label": str(r["verifier_metadata"]["oracle"][key]),
                       "messages": r["responses_create_params"]["input"],
                       "verifier_metadata": json.dumps(r["verifier_metadata"], ensure_ascii=False)}
                files[split].write(json.dumps(row, ensure_ascii=False) + "\n")
                counts[(env, split)] = counts.get((env, split), 0) + 1
        for f in files.values():
            f.close()
    shutil.copy(os.path.join(HERE, "data", "tasks", "stats.json"), os.path.join(OUT, "stats.json"))
    for k in sorted(counts):
        print(k, counts[k])


if __name__ == "__main__":
    main()
