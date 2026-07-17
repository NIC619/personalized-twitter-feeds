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
| V4 | Interests-only | yes | V3 + RAG feedback context |
| V5 | Persona-driven | yes | Protocol-architect voice, refined interest map (FOCIL, ePBS, TEE/AI…), signal/noise criteria |
| V6 | Binary decision | yes | Forces 70+ or 0-49, eliminates ambiguous middle scores |
| V7 | Negative-first | yes | Leads with skip criteria, "when in doubt, skip" |

"RAG: yes" means the prompt has a `{rag_context}` placeholder. If RAG is disabled
(or has no data yet), the placeholder is filled with *"No user feedback context
available yet."* — the prompt still runs, but you're not testing its feedback loop.
`/ab_info` warns you when this mismatch is active.

## Config (env vars, set in Railway Variables + local `.env`)

```env
CONTROL_PROMPT=auto            # Production prompt. 'auto' = V1 (V2 when RAG available).
                               # Pin a key (e.g. V5) to promote an A/B winner — no code change.
AB_TEST_ENABLED=true
AB_TEST_EXPERIMENT_ID=exp_004  # FRESH id per experiment — results are grouped by this
AB_TEST_CHALLENGER_PROMPT=V5   # Registry key for the shadow prompt
RAG_ENABLED=true
```

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

| ID | Control | Challenger | Outcome |
|----|---------|------------|---------|
| exp_001 | V1 | V3 (interests-only) | — |
| exp_002 | V1/V2 | V3 | — |
| exp_003 | V1/V2 | V4 (interests + RAG) | — |

(Fill in outcomes as rounds conclude; `/ab_info` lists the raw data anytime.)

## Adding New Prompts

1. Write the prompt string in `src/claude_filter.py` (must contain `{tweets_json}`; add `{rag_context}` if it should use feedback context)
2. Add it to `PROMPT_REGISTRY` with the next key (e.g. `"V8"`)
3. Add a one-liner to `PROMPT_DESCRIPTIONS` (shown in `/ab_info` and reports)
4. Add a row to the registry table above
5. Commit, push, then set `AB_TEST_CHALLENGER_PROMPT=V8` + a fresh `AB_TEST_EXPERIMENT_ID` in Railway
