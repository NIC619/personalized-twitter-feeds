#!/usr/bin/env python3
"""A/B test report for comparing Claude filter prompts.

Queries paired scores + feedback and produces analysis including:
- Average score gap (upvoted vs downvoted) per prompt
- Precision/recall at threshold
- Wilcoxon signed-rank test for statistical significance
"""

import sys
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def run_ab_report(db, experiment_id: str, threshold: int = 70) -> None:
    """Generate and print an A/B test report.

    Args:
        db: DatabaseClient instance
        experiment_id: Experiment identifier
        threshold: Score threshold for precision/recall calculation
    """
    print(f"\n{'='*60}")
    print(f"A/B Test Report: {experiment_id}")
    print(f"{'='*60}\n")

    results = db.get_ab_test_results(experiment_id)

    if not results:
        print("No data found for this experiment.")
        print("Make sure tweets have been scored with A/B testing enabled.")
        return

    total_pairs = len(results)
    voted = [r for r in results if r.get("user_vote")]
    upvoted = [r for r in voted if r["user_vote"] == "up"]
    downvoted = [r for r in voted if r["user_vote"] == "down"]

    print(f"Total paired scores: {total_pairs}")
    print(f"Tweets with feedback: {len(voted)} ({len(upvoted)} up, {len(downvoted)} down)")
    print()

    # --- Score distributions ---
    control_scores = [r["control_score"] for r in results]
    challenger_scores = [r["challenger_score"] for r in results]
    control_prompt = results[0]["control_prompt"] if results else "?"
    challenger_prompt = results[0]["challenger_prompt"] if results else "?"

    print(f"Control prompt: {control_prompt}")
    print(f"  Avg score: {_mean(control_scores):.1f}")
    print(f"  Score range: {min(control_scores):.0f} - {max(control_scores):.0f}")
    print()
    print(f"Challenger prompt: {challenger_prompt}")
    print(f"  Avg score: {_mean(challenger_scores):.1f}")
    print(f"  Score range: {min(challenger_scores):.0f} - {max(challenger_scores):.0f}")
    print()

    # --- Score gap analysis (upvoted vs downvoted) ---
    if voted:
        print(f"{'='*60}")
        print("Score Gap Analysis (higher gap = better discrimination)")
        print(f"{'='*60}\n")

        for label, prompt_key, score_key in [
            ("Control", control_prompt, "control_score"),
            ("Challenger", challenger_prompt, "challenger_score"),
        ]:
            up_scores = [r[score_key] for r in upvoted] if upvoted else []
            down_scores = [r[score_key] for r in downvoted] if downvoted else []

            avg_up = _mean(up_scores) if up_scores else 0
            avg_down = _mean(down_scores) if down_scores else 0
            gap = avg_up - avg_down

            print(f"{label} ({prompt_key}):")
            print(f"  Avg score on upvoted tweets:   {avg_up:.1f} (n={len(up_scores)})")
            print(f"  Avg score on downvoted tweets:  {avg_down:.1f} (n={len(down_scores)})")
            print(f"  Score gap (up - down):          {gap:+.1f}")
            print()

    # --- Precision / Recall at threshold ---
    if voted:
        print(f"{'='*60}")
        print(f"Precision / Recall at threshold={threshold}")
        print(f"{'='*60}\n")

        for label, score_key in [("Control", "control_score"), ("Challenger", "challenger_score")]:
            tp = sum(1 for r in voted if r[score_key] >= threshold and r["user_vote"] == "up")
            fp = sum(1 for r in voted if r[score_key] >= threshold and r["user_vote"] == "down")
            fn = sum(1 for r in voted if r[score_key] < threshold and r["user_vote"] == "up")
            tn = sum(1 for r in voted if r[score_key] < threshold and r["user_vote"] == "down")

            precision = tp / (tp + fp) if (tp + fp) > 0 else 0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

            print(f"{label}:")
            print(f"  TP={tp} FP={fp} FN={fn} TN={tn}")
            print(f"  Precision: {precision:.2%}")
            print(f"  Recall:    {recall:.2%}")
            print(f"  F1:        {f1:.2%}")
            print()

    # --- Wilcoxon signed-rank test ---
    if len(voted) >= 5:
        print(f"{'='*60}")
        print("Statistical Test (Wilcoxon signed-rank)")
        print(f"{'='*60}\n")

        try:
            from scipy.stats import wilcoxon

            # Test if the score differences between control and challenger are significant
            diffs = [r["challenger_score"] - r["control_score"] for r in voted]
            non_zero_diffs = [d for d in diffs if d != 0]

            if len(non_zero_diffs) >= 5:
                stat, p_value = wilcoxon(non_zero_diffs)
                print(f"Paired differences (challenger - control) on voted tweets:")
                print(f"  Mean difference: {_mean(diffs):+.1f}")
                print(f"  Wilcoxon statistic: {stat:.1f}")
                print(f"  p-value: {p_value:.4f}")
                if p_value < 0.05:
                    direction = "higher" if _mean(diffs) > 0 else "lower"
                    print(f"  Result: Significant (p<0.05) — challenger scores {direction}")
                else:
                    print(f"  Result: Not significant (p={p_value:.3f})")
            else:
                print(f"Not enough non-zero differences ({len(non_zero_diffs)}) for Wilcoxon test.")
                print("Need at least 5 voted tweets with different scores between prompts.")
        except ImportError:
            print("scipy not installed — skipping Wilcoxon test.")
            print("Install with: pip install scipy")
        print()

    # --- Recommendation ---
    print(f"{'='*60}")
    print("Recommendation")
    print(f"{'='*60}\n")

    if len(voted) < 10:
        print(f"Insufficient feedback ({len(voted)} votes). Need at least 10-20 for a useful signal.")
        print("Keep running the experiment and voting on tweets.")
    elif voted:
        # Compare F1 scores
        control_tp = sum(1 for r in voted if r["control_score"] >= threshold and r["user_vote"] == "up")
        control_fp = sum(1 for r in voted if r["control_score"] >= threshold and r["user_vote"] == "down")
        control_fn = sum(1 for r in voted if r["control_score"] < threshold and r["user_vote"] == "up")
        c_prec = control_tp / (control_tp + control_fp) if (control_tp + control_fp) > 0 else 0
        c_rec = control_tp / (control_tp + control_fn) if (control_tp + control_fn) > 0 else 0
        c_f1 = 2 * c_prec * c_rec / (c_prec + c_rec) if (c_prec + c_rec) > 0 else 0

        chall_tp = sum(1 for r in voted if r["challenger_score"] >= threshold and r["user_vote"] == "up")
        chall_fp = sum(1 for r in voted if r["challenger_score"] >= threshold and r["user_vote"] == "down")
        chall_fn = sum(1 for r in voted if r["challenger_score"] < threshold and r["user_vote"] == "up")
        ch_prec = chall_tp / (chall_tp + chall_fp) if (chall_tp + chall_fp) > 0 else 0
        ch_rec = chall_tp / (chall_tp + chall_fn) if (chall_tp + chall_fn) > 0 else 0
        ch_f1 = 2 * ch_prec * ch_rec / (ch_prec + ch_rec) if (ch_prec + ch_rec) > 0 else 0

        if ch_f1 > c_f1 + 0.05:
            print(f"Challenger ({challenger_prompt}) outperforms control ({control_prompt}).")
            print(f"F1: {ch_f1:.2%} vs {c_f1:.2%}")
            print("Consider promoting the challenger to production.")
        elif c_f1 > ch_f1 + 0.05:
            print(f"Control ({control_prompt}) outperforms challenger ({challenger_prompt}).")
            print(f"F1: {c_f1:.2%} vs {ch_f1:.2%}")
            print("Keep the current production prompt.")
        else:
            print(f"No clear winner. F1: control={c_f1:.2%}, challenger={ch_f1:.2%}")
            print("Consider gathering more data or trying a different challenger prompt.")
    print()


def _mean(values: list) -> float:
    """Calculate mean of a list of numbers."""
    return sum(values) / len(values) if values else 0.0


if __name__ == "__main__":
    import argparse
    from config.settings import get_settings
    from src.database import DatabaseClient

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="A/B test report")
    parser.add_argument("experiment_id", help="Experiment ID to analyze")
    parser.add_argument("--threshold", type=int, default=70, help="Score threshold")
    args = parser.parse_args()

    settings = get_settings()
    db = DatabaseClient(url=settings.supabase_url, key=settings.supabase_key)
    run_ab_report(db, args.experiment_id, threshold=args.threshold)
