# A/B Testing Plan: Claude Filter Prompts

## How It Works

Every run, tweets are scored by **two** prompts: the control (production, determines what gets sent) and the challenger (shadow, scores are saved but invisible to the user). After collecting enough thumbs up/down votes, run the report to see which prompt better predicts your preferences.

## Prompt Registry

| Key | Style | Description |
|-----|-------|-------------|
| V1 | Bio + rubric | Full background paragraph + detailed scoring tiers (original, no RAG) |
| V2 | Bio + rubric + RAG | Same as V1 but injects similar voted tweets as context |
| V3 | Interests-only | No bio — just a prioritized topic list with score ranges |
| V4 | Binary decision | Forces 70+ or 0-49, eliminates ambiguous middle scores |
| V5 | Negative-first | Leads with skip criteria, "when in doubt, skip" philosophy |

## Test Rounds

### Round 1: V1 vs V3 (RAG off)

Tests whether stripping the bio and using a clean topic list scores better than the original prompt.

```env
AB_TEST_ENABLED=true
AB_TEST_EXPERIMENT_ID=exp_001
AB_TEST_CHALLENGER_PROMPT=V3
RAG_ENABLED=false
```

### Round 2 (if V3 wins): V2 vs V3 (RAG on for control only)

Tests whether V1+RAG can beat the interests-only format.

```env
AB_TEST_ENABLED=true
AB_TEST_EXPERIMENT_ID=exp_002
AB_TEST_CHALLENGER_PROMPT=V3
RAG_ENABLED=true
```

### Round 2 (if V1 wins): V1 vs V4 or V5

If the bio+rubric format is better, test whether forcing binary decisions (V4) or a strict skip-first approach (V5) improves precision.

```env
AB_TEST_EXPERIMENT_ID=exp_002
AB_TEST_CHALLENGER_PROMPT=V4  # or V5
RAG_ENABLED=false
```

## Running an Experiment

1. Set the env vars above in `.env`
2. Run normally: `python main.py --once`
3. Vote on tweets in Telegram (thumbs up/down)
4. Repeat for ~30-50 voted tweets
5. Generate report: `python main.py --ab-report exp_001`
6. Promote the winner, start next round

## Reading the Report

The report shows:

- **Score gap**: Average score on upvoted vs downvoted tweets per prompt. Bigger gap = better discrimination.
- **Precision/Recall/F1**: At the configured threshold (default 70). Higher F1 = better overall.
- **Wilcoxon signed-rank test**: Statistical significance of the difference between prompts (needs scipy).
- **Recommendation**: Based on F1 comparison with a 5% margin.

## Adding New Prompts

1. Write the prompt string in `src/claude_filter.py` (use `{tweets_json}` placeholder, optionally `{rag_context}`)
2. Add it to `PROMPT_REGISTRY` with a new key (e.g. `"V6"`)
3. Set `AB_TEST_CHALLENGER_PROMPT=V6` in `.env`
