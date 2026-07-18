# A/B Testing: Claude Filter Prompts

## How It Works

Every run, content is scored by **two** prompts:

- **Control** — the production prompt. Its scores decide what actually gets sent to Telegram.
- **Challenger** — runs in shadow mode. Its scores are saved to `ab_test_scores` but never affect what you see.

After collecting enough thumbs up/down votes, the report shows which prompt better predicts your preferences.

## Prompt Registry

Source of truth: `PROMPT_REGISTRY` + `PROMPT_DESCRIPTIONS` in `src/claude_filter.py`.
(`/ab_info` in Telegram prints this same table — keep all three in sync when adding prompts.)

| Key | Style | RAG | Description |
|-----|-------|-----|-------------|
| V1 | Bio + rubric | no | Full background paragraph + detailed scoring tiers (original production) |
| V2 | Bio + rubric | yes | V1 + similar voted tweets injected as context |
| V3 | Interests-only | no | No bio — just a prioritized topic list with score ranges |
| V4 | Interests-only | yes | V3 + RAG feedback context. **exp_003 winner** |
| V5 | Binary decision | yes | V4's interest list, but forces 70+ or 0-49 — bans the 50-69 dead zone |
| V6 | Reason-first | yes | V4, but the model writes its reason *before* the score in each JSON object |
| V7 | Refreshed interests | yes | V4 + current topic map (FOCIL, ePBS, execution tickets, intents, TEE/AI agents) |

V5–V7 each change exactly **one variable** relative to V4, targeting exp_003's
diagnosis: precision was already 100%, recall only ~55% — good content was
being under-scored into 50-69, not noise getting through. They are numbered
in the planned test order: V5 next, then V6, then V7. (The original V5
persona / V6 bio-binary / V7 strict prompts were replaced untested on
2026-07-18; the strict prompt optimized precision, which the feedback loop
can't even measure since it only sees sent tweets.)

"RAG: yes" means the prompt has a `{rag_context}` placeholder. If RAG is disabled
(or has no data yet), the placeholder is filled with *"No user feedback context
available yet."* — the prompt still runs, but you're not testing its feedback loop.
`/ab_info` warns you when this mismatch is active.

## Config (env vars, set in Railway Variables + local `.env`)

```env
CONTROL_PROMPT=V2              # Production prompt (registry key, default V2). Set to an
                               # A/B winner (e.g. V5) to promote it — no code change.
AB_TEST_ENABLED=true
AB_TEST_EXPERIMENT_ID=exp_004  # FRESH id per experiment — results are grouped by this
AB_TEST_CHALLENGER_PROMPT=V5   # Registry key for the shadow prompt
RAG_ENABLED=true
```

Historical note: before exp_004 the control was hardcoded to switch per run —
V2 when RAG context was available, V1 otherwise — which is why exp_001–003
show `V1/V2` as the control. Since then the control is exactly
`CONTROL_PROMPT`; a RAG prompt without context just gets the fallback line.

Prompt keys are validated at startup — a typo fails the deploy loudly instead of
silently collecting no data.

## Running an Experiment

1. Pick a challenger and a **fresh** experiment ID; set the env vars in Railway and redeploy
2. Use the bot normally and vote 👍/👎 on tweets/blog posts
3. Repeat for ~30–50 voted items
4. Check results: `/ab_report` in Telegram (defaults to the current experiment),
   or locally `python main.py --ab-report exp_004`
5. Promote the winner: set `CONTROL_PROMPT=<winner>` (and turn off or re-point the A/B test)
6. Start the next round with a new experiment ID

## Telegram Commands

- `/ab_info` — current config, RAG status/warnings, all past experiments, prompt legend
- `/ab_report` — full report for the current experiment
- `/ab_report 60` — same, at threshold 60
- `/ab_report exp_002 [threshold]` — report for a past experiment

## Reading the Report

- **Score gap**: Average score on upvoted vs downvoted items per prompt. Bigger gap = better discrimination.
- **Precision/Recall/F1**: At the configured threshold (default 70). Higher F1 = better overall.
- **Wilcoxon signed-rank test**: Statistical significance of the difference between prompts (needs scipy and ≥5 voted items with differing scores).
- **Recommendation**: Based on F1 comparison with a 5% margin; needs 10+ votes to say anything.

## Experiment History

**Record outcomes here when a round concludes** — raw scores for experiments
older than current + previous are auto-deleted (see Retention below), so this
table is the only place old results survive.

| ID | Period | Control | Challenger | Votes | Outcome |
|----|--------|---------|------------|-------|---------|
| exp_001 | 2026-03-01 → 03-17 | V1 | V3 (interests-only) | 76 | No clear winner — F1 82.64% vs 82.88%; challenger scored significantly *lower* (Wilcoxon p<0.05) |
| exp_002 | 2026-03-18 → 04-20 | V1/V2 | V3 | 185 | No clear winner — F1 62.55% vs 66.93% (p=0.189) |
| exp_003 | 2026-04-20 → 07-18 | V1/V2 | V4 (interests + RAG) | 331 | **V4 wins** — F1 70.97% vs 61.47%, same precision, recall 55% vs 44% (p<0.001) |

## Retention

`ab_test_scores` grows ~90 rows/day, so after each daily curation run the
scheduler deletes scores from experiments other than the **current one
(`AB_TEST_EXPERIMENT_ID`) and the most recently active other one** — i.e.
you can always still run `/ab_report <previous>` for one round back.
Anything older is gone; record its outcome in the history table above first.

## Adding New Prompts

1. Write the prompt string in `src/claude_filter.py` (must contain `{tweets_json}`; add `{rag_context}` if it should use feedback context)
2. Add it to `PROMPT_REGISTRY` with the next key (e.g. `"V8"`)
3. Add a one-liner to `PROMPT_DESCRIPTIONS` (shown in `/ab_info` and reports)
4. Add a row to the registry table above
5. Commit, push, then set `AB_TEST_CHALLENGER_PROMPT=V8` + a fresh `AB_TEST_EXPERIMENT_ID` in Railway
