"""Five tool functions. Imports db and integrations, never Slack.

Which agent may call which tool is decided in agents.py, not here. Sending
email is deliberately absent: draft_email returns a draft and stops.
"""

import os
from collections import Counter
from datetime import datetime, timedelta, timezone

from agents import function_tool
from github import Auth, Github

from src import db

PRIORITY_BY_LEVEL = {5: "P0", 4: "P1", 3: "P2", 2: "P3", 1: "P3"}
STRUCTURAL_FLOOR = 4  # structural_risk at or above this is P0 regardless of impact


_gh = None


def _repo():
    global _gh
    if _gh is None:  # one client, one keep-alive session, across the process
        _gh = Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"]))
    # lazy: skip the repo GET; issues and creates work on the name alone.
    return _gh.get_repo(os.environ["GITHUB_REPO"], lazy=True)


# --- Analyst ------------------------------------------------------------------

@function_tool
def query_metrics(metric: str, days: int = 14) -> dict:
    """Read one metric's daily series and compare the last 24h to the prior baseline.

    Args:
        metric: Metric name, e.g. "checkout_success" or "signups".
        days: How many days of history to read. Default 14.
    """
    rows = db.query_metrics(metric, days)
    # Same windows as db.window_deltas: last 24h vs the 7 days before that, so
    # the analyst reasons over the numbers the detector flagged.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = [float(r["value"]) for r in rows if r["recorded_at"] >= cutoff]
    prior = [
        float(r["value"])
        for r in rows
        if cutoff - timedelta(days=7) <= r["recorded_at"] < cutoff
    ]
    baseline = round(sum(prior) / len(prior), 2) if prior else None
    current = round(sum(recent) / len(recent), 2) if recent else None
    pct_change = (
        round((current - baseline) / baseline * 100, 1)
        if baseline and current is not None
        else None
    )
    return {
        "metric": metric,
        "days": days,
        "baseline": baseline,
        "current": current,
        "pct_change": pct_change,
        "series": [
            {"date": r["recorded_at"].date().isoformat(), "value": float(r["value"])}
            for r in rows
        ],
    }


# --- CTO ----------------------------------------------------------------------

@function_tool
def list_open_issues() -> dict:
    """List open GitHub issues, how many each assignee holds, and who is least loaded."""
    # The list payload only carries a pull_request key for PRs. Reading
    # `.pull_request` makes PyGithub re-fetch every plain issue (~1s each), so
    # look at the raw payload instead.
    issues = [i for i in _repo().get_issues(state="open") if "pull_request" not in i._rawData]
    load = Counter()
    for i in issues:
        for a in i.assignees:
            load[a.login] += 1
    # Computed here, not by the model: fewest open issues, ties broken by login.
    least_loaded = min(sorted(load), key=load.__getitem__) if load else None
    return {
        "open_count_by_assignee": dict(load),
        "least_loaded": least_loaded,
        "issues": [
            {
                "number": i.number,
                "title": i.title,
                "assignees": [a.login for a in i.assignees],
                "url": i.html_url,
            }
            for i in issues
        ],
    }


def score_priority(behavioral_impact: int, structural_risk: int) -> dict:
    """Deterministic priority from two 1-5 axes. structural_risk >= 4 floors at P0.

    Args:
        behavioral_impact: How many users notice, and how badly. 1 = none, 5 = most users blocked.
        structural_risk: Whether the fix crosses an architectural boundary. 1 = local patch, 5 = core boundary.
    """
    for name, v in (("behavioral_impact", behavioral_impact), ("structural_risk", structural_risk)):
        if not 1 <= v <= 5:
            raise ValueError(f"{name} must be 1-5, got {v}")

    if structural_risk >= STRUCTURAL_FLOOR:
        return {
            "priority": "P0",
            "driver": "structural_risk",
            "reason": (
                f"structural_risk={structural_risk} crosses an architectural boundary; "
                f"floored at P0 regardless of behavioral_impact={behavioral_impact}."
            ),
        }

    level = max(behavioral_impact, structural_risk)
    driver = "behavioral_impact" if behavioral_impact >= structural_risk else "structural_risk"
    return {
        "priority": PRIORITY_BY_LEVEL[level],
        "driver": driver,
        "reason": (
            f"{driver}={level} set the level; "
            f"behavioral_impact={behavioral_impact}, structural_risk={structural_risk} (local patch)."
        ),
    }


score_priority_tool = function_tool(score_priority)


@function_tool
def create_issue(title: str, body: str, assignee: str) -> dict:
    """Create a real GitHub issue. Returns its URL.

    Args:
        title: Issue title. Prefix with the priority, e.g. "[P0] ...".
        body: Markdown body: what broke, both scores, which axis drove the priority, the architectural reason.
        assignee: GitHub login of the engineer taking it.
    """
    issue = _repo().create_issue(title=title, body=body, assignee=assignee)
    return {"url": issue.html_url, "number": issue.number, "assignee": assignee}


# --- Comms --------------------------------------------------------------------

@function_tool
def draft_email(subject: str, body: str) -> dict:
    """Draft a customer email. Returns the draft. Sends nothing; a human approves in Slack.

    Args:
        subject: Email subject line.
        body: Plain-text body, first person, under 120 words.
    """
    return {
        "subject": subject,
        "body": body,
        "word_count": len(body.split()),
        "status": "draft — not sent",
    }
