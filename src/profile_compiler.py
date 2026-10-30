"""Compile recorded signals into per-domain preference profiles.

The profiles replace the hand-written interest tiers: they are generated
from what the reader actually voted on (with their confirmed reasons and
notes), stored versioned in ``preference_profiles``, injected into the
scoring prompt, and exported as PREFERENCES.md for any other agent.
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from src.claude_filter import DOMAINS

logger = logging.getLogger(__name__)

# How much evidence goes into one domain's prompt
MAX_UP_EXAMPLES = 120
MAX_DOWN_EXAMPLES = 60
MAX_NOTES = 40
MIN_VOTES_FOR_DOMAIN = 8
# Scoring prompt budget for the compiled profile (characters)
SCORING_PROFILE_BUDGET = 7000


PROFILE_PROMPT = """You maintain a preference profile for one reader. Below is their voting record in the domain "{domain}": items they upvoted (🔥 = strongly), items they downvoted, their own stated reasons, and free-text notes. Write the profile section for this domain in Markdown.

Requirements:
- Write in second person ("You want…", "You skip…"), concrete and specific. Name sub-topics, angles, formats, authors and tells. Prefer "deep dives that quantify X" over "technical content".
- Sections, in order: `### What you want` (bulleted, most important first), `### What you skip`, `### Nuances` (edge cases, conditions, contradictions you resolved), `### Representative examples` (3-6 short quotes from the record with the vote).
- Weight the reader's own reasons and notes above your inference from item text. Weight 🔥 votes above plain upvotes. Recent votes slightly above old ones.
- Do not invent interests that the record does not support. If evidence is thin, say so in one line instead of padding.
- Target 250-500 words.

{previous_block}

## Voting record
{record}

Call write_profile with the Markdown."""


OVERALL_PROMPT = """You maintain a preference profile for one reader. Below are their per-domain profiles and the share of their votes in each domain. Write a short `## Overall` section (120-220 words, Markdown, second person): what unites what they value across domains, how they allocate attention between domains, and the two or three strongest cross-cutting tells for "worth their time" versus "skip". Do not repeat the per-domain bullets.

## Vote share by domain
{shares}

## Domain profiles
{profiles}

Call write_profile with the Markdown."""


WRITE_TOOL = {
    "name": "write_profile",
    "description": "Submit the profile Markdown.",
    "input_schema": {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    },
}


def group_signals(rows: list[dict]) -> dict[str, dict]:
    """Group signal rows (with embedded item) by domain.

    Returns:
        {domain: {"up": [...], "down": [...], "notes": [...]}} where each
        entry is {text, reason, strength, created_at, author}.
    """
    groups: dict[str, dict] = defaultdict(lambda: {"up": [], "down": [], "notes": []})
    for row in rows:
        item = row.get("items") or {}
        domain = item.get("domain") or "other"
        text = (item.get("title") or "").strip()
        body = (item.get("text") or "").strip()
        if body and body != text:
            text = f"{text}\n{body}" if text else body
        entry = {
            "text": text[:400],
            "reason": row.get("reason"),
            "strength": row.get("strength") or 1,
            "created_at": row.get("created_at"),
            "author": item.get("author_username"),
        }
        if row["kind"] == "vote" and row.get("value") in ("up", "down"):
            groups[domain][row["value"]].append(entry)
        elif row["kind"] == "note" and row.get("reason"):
            groups[domain]["notes"].append(entry)
    return dict(groups)


def format_record(group: dict) -> str:
    """Render one domain's evidence for the prompt (newest first as given)."""
    lines = []
    ups = sorted(group["up"], key=lambda e: -e["strength"])[:MAX_UP_EXAMPLES]
    downs = group["down"][:MAX_DOWN_EXAMPLES]
    notes = group["notes"][:MAX_NOTES]

    lines.append(f"### Upvoted ({len(group['up'])} total, showing {len(ups)})")
    for e in ups:
        mark = "🔥" if e["strength"] >= 2 else "👍"
        reason = f' — reason: "{e["reason"]}"' if e.get("reason") else ""
        author = f" (@{e['author']})" if e.get("author") else ""
        lines.append(f"- {mark}{author}: {_one_line(e['text'])}{reason}")
    lines.append("")
    lines.append(f"### Downvoted ({len(group['down'])} total, showing {len(downs)})")
    for e in downs:
        reason = f' — reason: "{e["reason"]}"' if e.get("reason") else ""
        author = f" (@{e['author']})" if e.get("author") else ""
        lines.append(f"- 👎{author}: {_one_line(e['text'])}{reason}")
    if notes:
        lines.append("")
        lines.append(f"### Notes the reader wrote ({len(notes)})")
        for e in notes:
            lines.append(f'- "{e["reason"]}" — on: {_one_line(e["text"])[:120]}')
    return "\n".join(lines)


def _one_line(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


class ProfileCompiler:
    """Builds and stores preference profiles from signals."""

    def __init__(self, claude_client, model: str, db):
        self.client = claude_client
        self.model = model
        self.db = db

    def _ask(self, prompt: str) -> Optional[str]:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            temperature=0.2,
            tools=[WRITE_TOOL],
            tool_choice={"type": "tool", "name": "write_profile"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use" and block.name == "write_profile":
                return str(block.input.get("content", "")).strip() or None
        return None

    def compile_domain(self, domain: str, group: dict, previous: Optional[str]) -> Optional[str]:
        previous_block = (
            "## Previous version of this section (revise it; keep what still holds)\n" + previous
            if previous else ""
        )
        prompt = PROFILE_PROMPT.format(
            domain=domain, previous_block=previous_block, record=format_record(group),
        )
        return self._ask(prompt)

    def compile_overall(self, profiles: dict[str, str], shares: dict[str, float]) -> Optional[str]:
        shares_text = "\n".join(
            f"- {d}: {100 * s:.0f}%" for d, s in sorted(shares.items(), key=lambda kv: -kv[1])
        )
        profiles_text = "\n\n".join(f"## {d}\n{c}" for d, c in profiles.items())
        return self._ask(OVERALL_PROMPT.format(shares=shares_text, profiles=profiles_text))

    def compile_all(self, min_votes: int = MIN_VOTES_FOR_DOMAIN) -> dict[str, str]:
        """Compile every domain with enough votes plus the overall section.

        Returns:
            {domain: markdown} for everything written (including 'overall').
        """
        rows = self.db.get_signals_for_profile()
        groups = group_signals(rows)
        previous = self.db.get_latest_profiles()

        total_votes = sum(len(g["up"]) + len(g["down"]) for g in groups.values()) or 1
        written: dict[str, str] = {}
        shares: dict[str, float] = {}
        for domain in sorted(groups, key=lambda d: DOMAINS.index(d) if d in DOMAINS else 99):
            group = groups[domain]
            n = len(group["up"]) + len(group["down"])
            shares[domain] = n / total_votes
            if n < min_votes:
                logger.info(f"Profile: skipping {domain} ({n} votes < {min_votes})")
                continue
            logger.info(f"Profile: compiling {domain} ({len(group['up'])} up, {len(group['down'])} down)")
            prev_content = (previous.get(domain) or {}).get("content")
            content = self.compile_domain(domain, group, prev_content)
            if not content:
                logger.warning(f"Profile: empty result for {domain}")
                continue
            stats = {
                "up": len(group["up"]), "down": len(group["down"]),
                "notes": len(group["notes"]), "share": round(shares[domain], 3),
            }
            self.db.save_profile(domain, content, stats)
            written[domain] = content

        if written:
            overall = self.compile_overall(written, shares)
            if overall:
                self.db.save_profile("overall", overall, {"domains": list(written), "votes": total_votes})
                written["overall"] = overall
        return written


def render_preferences_md(profiles: dict[str, dict]) -> str:
    """Render the latest profiles as a single Markdown document."""
    lines = ["# Preferences", ""]
    lines.append(
        f"_Compiled from recorded votes, reasons and notes. "
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')}._"
    )
    lines.append("")
    overall = profiles.get("overall")
    if overall:
        lines.append(overall["content"].strip())
        lines.append("")
    for domain in sorted(
        (d for d in profiles if d != "overall"),
        key=lambda d: DOMAINS.index(d) if d in DOMAINS else 99,
    ):
        row = profiles[domain]
        stats = row.get("stats") or {}
        meta = f" _(v{row.get('version')}, {stats.get('up', '?')} up / {stats.get('down', '?')} down)_"
        lines.append(f"## {domain}{meta}")
        lines.append("")
        lines.append(row["content"].strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def scoring_profile(profiles: dict[str, dict], fallback: str, budget: int = SCORING_PROFILE_BUDGET) -> str:
    """Compact profile text for the scoring prompt, within a character budget.

    Overall first, then domains by vote share; each domain is trimmed to its
    "What you want" / "What you skip" sections if the budget is tight.
    """
    if not profiles:
        return fallback
    parts: list[str] = []
    overall = profiles.get("overall")
    if overall:
        parts.append(overall["content"].strip())
    domains = [d for d in profiles if d != "overall"]
    domains.sort(key=lambda d: -float((profiles[d].get("stats") or {}).get("share") or 0))
    used = sum(len(p) for p in parts)
    for d in domains:
        content = profiles[d]["content"].strip()
        section = f"## {d}\n{content}"
        if used + len(section) > budget:
            section = f"## {d}\n{_core_sections(content)}"
            if used + len(section) > budget:
                continue
        parts.append(section)
        used += len(section)
    return "\n\n".join(parts) if parts else fallback


def _core_sections(markdown: str) -> str:
    """Keep only the want/skip sections of a domain profile."""
    keep: list[str] = []
    keeping = False
    for line in markdown.splitlines():
        if line.startswith("### "):
            keeping = ("want" in line.lower()) or ("skip" in line.lower())
        if keeping:
            keep.append(line)
    return "\n".join(keep).strip() or markdown[:1500]
