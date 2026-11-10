# Running Sven on Berzelius

Files here are copied to `/proj/PROJECT/users/USER/sven-myndighet/` (a new folder; nothing else is touched).

```bash
# from the Mac, with the SSH session open
B=/proj/PROJECT/users/USER/sven-myndighet
ssh berzelius "mkdir -p $B/slurm $B/data"
rsync -av setup.sh eval.sbatch train.sbatch sven_eval.py berzelius:$B/slurm/
rsync -av ../sven_dataset/{train,dev,heldout}.jsonl ../sven_dataset/stats.json berzelius:$B/data/

# on the login node, once (downloads code, Python packages and two small models to /proj)
bash $B/slurm/setup.sh

sbatch $B/slurm/eval.sbatch                 # 1 GPU, <= 45 min: Sven-0.6B zero-shot on heldout
sbatch $B/slurm/train.sbatch 200            # 1 GPU: smoke test of the training loop
sbatch $B/slurm/train.sbatch                # 1 GPU, <= 3 h: 2 epochs, then heldout scores
```

Verified on 2026-11-10: Kev at HEAD loads the Sven adapter, `kev.train --init_from` works offline with a cached hub id, and
the 0.6B (89 min) and Kev-4B (about 6.5 h) runs finished. `train.sbatch` is driven by environment variables (NAME, BASE, INIT, DATA, EPOCHS,
BATCH, ACCUM, STEPS). Always pass `--max_state 7552` (kev's default of 384 rejects most case files).

No replay data is mixed in yet. Sven v0.1 used 200 English replay records; add `--suite <frozen suite dir> --replay N`
once the upstream suite (`jaredpalmer/kev-suites`) is downloaded, so the adapter does not forget its general skills.
