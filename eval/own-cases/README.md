# Our own 100 test cases

Written 2026-11-26 in venorexai's case format (`eval/cases/*.yaml` in his repo), so both assistants can run on them and
the same judge (`eval/judge_venorex.py`) can score them.

| | Count |
|---|---|
| Split | 70 `dev` (to improve with), 30 `holdout` (hidden, for the final score only) |
| Language | English 30, Urdu 30, Arabic 20, Gulf Arabic 20 |
| Bucket | electrical 16, engine 14, CAN/LIN 10, EV 10, hybrid 10, incomplete info 10, exact-value requests 10, intermittent 8, charging/starting 6, safety 6 |
| Expected behaviour | answer 80, ask for information 10, abstain on the exact value 10 |
| Safety-critical | 39 |

- Every case names a make and model, so our assistant can open a session. The incomplete-info cases leave out the
  year, engine and codes.
- The holdout split is stratified by bucket, with 9 English, 9 Urdu, 6 Arabic and 6 Gulf Arabic cases.
- `review.status` is `ai_draft`. No expert is available.

## Review before use

1. **ChatGPT reviews each batch**, with web search on, using `REVIEW_PROMPT.md`. Make the 4 batch files with:
   `python -c "import pathlib as p; f=sorted(p.Path('eval/own-cases').glob('*/*.yaml'), key=lambda x: x.name); [p.Path(f'batch-{i+1}.yaml').write_text(chr(10).join('---'+chr(10)+x.read_text(encoding='utf-8') for x in f[i*25:i*25+25]), encoding='utf-8') for i in range(4)]"`
2. **Disagreements:** a line marked wrong or unsure is fixed or removed. Only points both AIs agree on stay. Then set
   `review.cross_checked_by: chatgpt` on each case.
3. **Ali checks the safety-critical cases himself** (`safety_critical: true`).
4. **Call the result "cross-checked by two AIs"**, never "verified".

Don't tune prompts on the `holdout` cases. Look at them only for the final score.
