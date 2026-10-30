#!/usr/bin/env python3
"""Offline evaluation of the scoring prompt against recorded votes.

Re-scores every voted item with the current prompt (any model, any profile
text, with or without per-item neighbours) and reports how well the scores
separate upvotes from downvotes. A run takes minutes, not months.

Examples:

    # Baseline: production model + profile + per-item neighbours
    python scripts/eval_prompt.py --label baseline

    # Same prompt without neighbours (isolates the RAG effect)
    python scripts/eval_prompt.py --label no-rag --no-rag

    # Another model, or another profile text
    python scripts/eval_prompt.py --label sonnet55 --model claude-sonnet-5-5
    python scripts/eval_prompt.py --label profile-v2 --profile-file profiles/v2.md

    # Side-by-side report of saved runs (plus the scores stored at send time)
    python scripts/eval_prompt.py --compare baseline no-rag sonnet55

Caveats the numbers inherit from the data:

* Votes exist only for items that were *sent*, so the set is biased toward
  content the old prompt already liked. Recall here means "share of upvoted
  items that would still be sent", not recall over the whole timeline.
  Exploration items (sent below threshold, 🎲) are the only unbiased sample
  and are reported separately once there are enough of them.
* Downvotes are ~9% of votes, so precision is noisy. AUC is the headline
  number: the probability a random upvote outscores a random downvote.
* Neighbour lookup excludes the item itself, but neighbours voted *after*
  the item still leak a little future information.
"""

import argparse
import json
import logging
import os
import random
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger("eval")

RUNS_DIR = Path("eval_runs")
BANDS = [(0, 49), (50, 59), (60, 69), (70, 79), (80, 89), (90, 100)]


# ----------------------------------------------------------------- metrics

def auc(pos: list[float], neg: list[float]) -> float | None:
    """Area under the ROC curve via the Mann-Whitney U statistic."""
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1
            elif p == n:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def metrics_at(rows: list[dict], threshold: int) -> dict:
    """Precision/recall/send rate at a threshold. rows: {score, vote}."""
    ups = [r for r in rows if r["vote"] == "up"]
    downs = [r for r in rows if r["vote"] == "down"]
    sent = [r for r in rows if r["score"] >= threshold]
    sent_up = sum(1 for r in sent if r["vote"] == "up")
    sent_down = sum(1 for r in sent if r["vote"] == "down")
    return {
        "threshold": threshold,
        "recall": sent_up / len(ups) if ups else None,
        "precision": sent_up / len(sent) if sent else None,
        "down_blocked": (len(downs) - sent_down) / len(downs) if downs else None,
        "send_rate": len(sent) / len(rows) if rows else None,
    }


def summarize(rows: list[dict], threshold: int) -> dict:
    """Full metric bundle for a list of {score, vote, reason?, exploration?}."""
    rows = [r for r in rows if r.get("score") is not None]
    ups = [r["score"] for r in rows if r["vote"] == "up"]
    downs = [r["score"] for r in rows if r["vote"] == "down"]

    bands = []
    for lo, hi in BANDS:
        in_band = [r for r in rows if lo <= r["score"] <= hi]
        n_up = sum(1 for r in in_band if r["vote"] == "up")
        bands.append({
            "band": f"{lo}-{hi}",
            "n": len(in_band),
            "up_share": n_up / len(in_band) if in_band else None,
        })

    by_reason: dict[str, dict] = {}
    for r in rows:
        if r["vote"] != "up":
            continue
        key = r.get("reason") or "(none)"
        entry = by_reason.setdefault(key, {"n": 0, "sent": 0})
        entry["n"] += 1
        if r["score"] >= threshold:
            entry["sent"] += 1
    for entry in by_reason.values():
        entry["recall"] = entry["sent"] / entry["n"] if entry["n"] else None

    exploration = [r for r in rows if r.get("exploration")]

    return {
        "n": len(rows),
        "n_up": len(ups),
        "n_down": len(downs),
        "auc": auc(ups, downs),
        "mean_up": statistics.mean(ups) if ups else None,
        "mean_down": statistics.mean(downs) if downs else None,
        "at_threshold": metrics_at(rows, threshold),
        "sweep": [metrics_at(rows, t) for t in (50, 60, 70, 80, 90)],
        "bands": bands,
        "by_reason": by_reason,
        "exploration": {
            "n": len(exploration),
            "n_up": sum(1 for r in exploration if r["vote"] == "up"),
        },
    }


# ----------------------------------------------------------------- scoring

def load_labeled(db, limit: int | None, seed: int) -> list[dict]:
    items = db.get_labeled_items()
    items.sort(key=lambda i: i["tweet_id"])
    if limit and limit < len(items):
        rng = random.Random(seed)
        items = rng.sample(items, limit)
    return items


def score_items(claude, items: list[dict], neighbors: dict, profile: str | None,
                workers: int) -> dict[str, dict]:
    """Score all items in parallel batches. Returns tweet_id -> {score, reason}."""
    batches = [items[i : i + claude.batch_size] for i in range(0, len(items), claude.batch_size)]
    out: dict[str, dict] = {}

    def _run(idx_batch):
        idx, batch = idx_batch
        logger.info(f"Scoring batch {idx + 1}/{len(batches)} ({len(batch)} items)")
        return claude._score_batch(batch, neighbors, profile or claude.profile)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for scores in pool.map(_run, enumerate(batches)):
            for s in scores:
                out[s["tweet_id"]] = s
    return out


def run_eval(args) -> Path:
    from config.settings import get_settings
    from src.claude_filter import ClaudeFilter
    from src.database import DatabaseClient
    from src.embeddings import EmbeddingManager

    settings = get_settings()
    db = DatabaseClient(url=settings.supabase_url, key=settings.supabase_key)

    profile = None
    if args.profile_file:
        profile = Path(args.profile_file).read_text()

    model = args.model or settings.claude_model
    claude = ClaudeFilter(
        api_key=settings.anthropic_api_key, model=model,
        batch_size=args.batch_size, profile=profile,
    )

    items = load_labeled(db, args.limit, args.seed)
    logger.info(f"Loaded {len(items)} labeled items")

    neighbors: dict = {}
    if not args.no_rag:
        emb = EmbeddingManager(
            api_key=settings.openai_api_key, model=settings.embedding_model, db_client=db,
        )
        # Voted items were embedded when voted, so read those vectors back
        # instead of paying to re-embed; only embed whatever is missing.
        vectors = db.get_embeddings([it["tweet_id"] for it in items])
        missing = [it for it in items if it["tweet_id"] not in vectors]
        logger.info(f"Stored embeddings for {len(vectors)}/{len(items)} items")
        if missing and emb.enabled:
            try:
                vectors.update(emb.embed_tweet_batch(missing))
            except Exception as e:
                logger.warning(f"Could not embed {len(missing)} items without stored vectors: {e}")
        elif missing:
            logger.warning(f"{len(missing)} items have no stored embedding and OPENAI_API_KEY is unset")
        logger.info(f"Looking up {args.k} neighbours per item...")
        neighbors = emb.find_neighbors(items, k=args.k, exclude_self=True, embeddings=vectors)
        logger.info(f"Neighbours found for {len(neighbors)}/{len(items)} items")

    scores = score_items(claude, items, neighbors, profile, args.workers)

    rows = []
    for it in items:
        s = scores.get(it["tweet_id"])
        rows.append({
            "tweet_id": it["tweet_id"],
            "vote": it["user_vote"],
            "reason": it.get("vote_reason"),
            "exploration": bool(it.get("exploration")),
            "stored_score": it.get("filter_score"),
            "score": s["score"] if s else None,
            "model_reason": s["reason"] if s else None,
            "author": it.get("author_username"),
            "text": (it.get("text") or "")[:200],
        })

    RUNS_DIR.mkdir(exist_ok=True)
    path = RUNS_DIR / f"{args.label}.json"
    payload = {
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "model": model,
            "profile_file": args.profile_file,
            "rag": not args.no_rag,
            "k": args.k,
            "batch_size": args.batch_size,
            "limit": args.limit,
            "seed": args.seed,
            "items_with_neighbors": len(neighbors),
        },
        "rows": rows,
    }
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    logger.info(f"Saved {len(rows)} scored rows to {path}")
    return path


# ----------------------------------------------------------------- report

def _pct(x):
    return "   -  " if x is None else f"{100 * x:5.1f}%"


def _num(x):
    return "  -  " if x is None else f"{x:5.1f}"


def format_report(runs: list[dict], threshold: int) -> str:
    """Side-by-side text report for saved runs (dicts with label, rows)."""
    summaries = [(r["label"], summarize(r["rows"], threshold), r.get("config", {})) for r in runs]
    lines = []
    lines.append(f"Offline eval @ threshold {threshold}")
    lines.append("")
    for label, s, cfg in summaries:
        desc = ", ".join(f"{k}={v}" for k, v in cfg.items() if k in ("model", "rag", "k", "profile_file") and v is not None)
        lines.append(f"  {label}: {desc}  (n={s['n']}, up={s['n_up']}, down={s['n_down']})")
    lines.append("")

    header = f"{'metric':<22}" + "".join(f"{l[:14]:>15}" for l, _, _ in summaries)
    lines.append(header)
    lines.append("-" * len(header))

    def row(name, getter, fmt):
        lines.append(f"{name:<22}" + "".join(f"{fmt(getter(s)):>15}" for _, s, _ in summaries))

    row("AUC", lambda s: s["auc"], lambda v: "  -  " if v is None else f"{v:.3f}")
    row("mean score (up)", lambda s: s["mean_up"], _num)
    row("mean score (down)", lambda s: s["mean_down"], _num)
    row(f"recall @{threshold}", lambda s: s["at_threshold"]["recall"], _pct)
    row(f"downs blocked @{threshold}", lambda s: s["at_threshold"]["down_blocked"], _pct)
    row(f"precision @{threshold}", lambda s: s["at_threshold"]["precision"], _pct)
    row(f"send rate @{threshold}", lambda s: s["at_threshold"]["send_rate"], _pct)
    lines.append("")

    lines.append("Threshold sweep (recall / downs blocked / send rate)")
    for t_idx, t in enumerate((50, 60, 70, 80, 90)):
        cells = []
        for _, s, _ in summaries:
            m = s["sweep"][t_idx]
            cells.append(f"{_pct(m['recall']).strip()}/{_pct(m['down_blocked']).strip()}/{_pct(m['send_rate']).strip()}")
        lines.append(f"  @{t:<20}" + "".join(f"{c:>15}" for c in cells))
    lines.append("")

    lines.append("Score bands: share of items in band that were upvoted (n)")
    for b_idx, (lo, hi) in enumerate(BANDS):
        cells = []
        for _, s, _ in summaries:
            b = s["bands"][b_idx]
            cells.append(f"{_pct(b['up_share']).strip()} ({b['n']})")
        lines.append(f"  {lo}-{hi:<19}" + "".join(f"{c:>15}" for c in cells))
    lines.append("")

    reasons = sorted({k for _, s, _ in summaries for k in s["by_reason"]})
    if reasons:
        lines.append(f"Recall @{threshold} by upvote reason")
        for key in reasons:
            cells = []
            for _, s, _ in summaries:
                e = s["by_reason"].get(key)
                cells.append(f"{_pct(e['recall']).strip()} ({e['n']})" if e else "-")
            lines.append(f"  {key[:20]:<21}" + "".join(f"{c:>15}" for c in cells))
        lines.append("")

    expl = [s["exploration"] for _, s, _ in summaries]
    if any(e["n"] for e in expl):
        lines.append("Exploration items (sent below threshold): "
                     + ", ".join(f"{l}: {e['n_up']}/{e['n']} upvoted" for (l, _, _), e in zip(summaries, expl)))
        lines.append("")

    lines.append("Caveat: labels exist only for items that were sent, so recall is "
                 "over previously-sent upvotes, and precision is inflated.")
    return "\n".join(lines)


def load_runs(labels: list[str]) -> list[dict]:
    runs = []
    for label in labels:
        path = RUNS_DIR / f"{label}.json"
        if not path.exists():
            raise SystemExit(f"No saved run '{label}' at {path}")
        runs.append(json.loads(path.read_text()))
    return runs


def stored_run(runs: list[dict]) -> dict | None:
    """Pseudo-run built from the scores stored when the items were sent."""
    base = runs[0]
    rows = [dict(r, score=r.get("stored_score")) for r in base["rows"] if r.get("stored_score") is not None]
    if not rows:
        return None
    return {"label": "stored@send", "config": {"model": "production at send time"}, "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", help="Name for this run (saved to eval_runs/<label>.json)")
    parser.add_argument("--model", default=None, help="Claude model (default: CLAUDE_MODEL setting)")
    parser.add_argument("--profile-file", default=None, help="Text file with an alternative preference profile")
    parser.add_argument("--no-rag", action="store_true", help="Score without per-item neighbours")
    parser.add_argument("--k", type=int, default=3, help="Neighbours per item (default 3)")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--workers", type=int, default=4, help="Parallel Claude calls")
    parser.add_argument("--limit", type=int, default=None, help="Random subset of labeled items")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--threshold", type=int, default=None, help="Report threshold (default: FILTER_THRESHOLD)")
    parser.add_argument("--compare", nargs="+", metavar="LABEL", help="Report saved runs side by side")
    parser.add_argument("--no-stored", action="store_true", help="Omit the stored-at-send-time column")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    threshold = args.threshold
    if threshold is None:
        threshold = int(os.environ.get("FILTER_THRESHOLD", "70"))

    labels = list(args.compare or [])
    if args.label:
        run_eval(args)
        if args.label not in labels:
            labels.append(args.label)
    if not labels:
        parser.error("give --label to run an eval and/or --compare LABEL... to report")

    runs = load_runs(labels)
    if not args.no_stored:
        stored = stored_run(runs)
        if stored:
            runs.insert(0, stored)
    print()
    print(format_report(runs, threshold))
    return 0


if __name__ == "__main__":
    sys.exit(main())
