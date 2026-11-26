# Safety sentence sets

Sentences written by another AI (ChatGPT, 2026-11-26) without seeing our rules, for `scripts/check_safety.py`.
Each line: `{"text", "language", "danger", "why"}`; 80 per set, 20 each in en / ar / ar_gulf / ur, half real danger.

- `set-1.jsonl`: the first set. **Used to improve the word list** (missing words were added after seeing it), so it
  is a regression check now, not an independent test.
- Set 2 stays on Ali's machine (`safety-set-2.jsonl`), unseen when the rules were written: the independent test.
  Result with both layers (commit `4f8d833`): 40/40 dangers stopped, 2/40 harmless messages stopped.
- For the next change to the safety rules, ask for a new set (same prompt, "use different situations and wording
  from any previous set") and keep it unseen until the change is done.

```
python scripts/check_safety.py eval/safety/set-1.jsonl --model
python scripts/check_safety.py --cases eval/own-cases --danger OC-CHG-02 --model   # ordinary questions not stopped
```
