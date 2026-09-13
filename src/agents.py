"""Three Agents, their prompts, and the handoffs between them. Never imports Slack.

Run the chain from the terminal (must be `-m` from the repo root, otherwise
this file shadows the `agents` SDK package):

    python -m src.agents real     # checkout_success, -34%: full chain, real issue
    python -m src.agents noise    # signups, -8%: analyst stops
"""

import json
import os

from agents import Agent, RunHooks, Runner, ToolCallItem, ToolCallOutputItem, handoff
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX
from pydantic import BaseModel, Field

from src import db
from src.tools import (
    create_issue,
    draft_email,
    list_open_issues,
    query_metrics,
    score_priority_tool,
)

# Small fast model for the analyst and comms, larger for the CTO's priority reasoning.
ANALYST_MODEL = os.environ.get("ANALYST_MODEL", "gpt-4.1-mini")
CTO_MODEL = os.environ.get("CTO_MODEL", "gpt-4.1")
COMMS_MODEL = os.environ.get("COMMS_MODEL", "gpt-4.1-mini")

MAX_TURNS = 20

FOUNDER_NAME = os.environ.get("FOUNDER_NAME", "the founder")


# Handoffs carry a typed payload. The model cannot route without stating its
# reasoning, so the lines the thread needs are guaranteed, not requested.

class Characterisation(BaseModel):
    characterisation: str = Field(
        description="Two or three sentences: how far below baseline, one-day collapse or "
        "slide, whether the prior days were stable, and why this is not ordinary variance."
    )


class CtoBrief(BaseModel):
    issue_url: str
    assignee: str = Field(description="Who takes it: the least_loaded login and their open count.")
    priority: str = Field(description='e.g. "P0"')
    driver: str = Field(description="Which axis drove the priority: behavioral_impact or structural_risk.")
    architectural_reason: str = Field(description="One sentence: the boundary the fix touches, or why it is a local patch.")
    customer_summary: str = Field(
        description="One line on what a user experienced, in their terms (e.g. 'payments at "
        "checkout have been failing since this morning'). No metric names, percentages, "
        "priorities, or architecture — this goes to customers."
    )


async def _noop(ctx, payload):
    pass


# Built bottom-up: each agent needs the ones it can hand to.

comms = Agent(
    name="Comms",
    model=COMMS_MODEL,
    tools=[draft_email],
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
You are Mia, comms. You draft; you never send. A human approves in Slack.

Write one short email from the founder to the users who hit the problem. First person,
plain words, no marketing voice. It must:
- open with an apology for the inconvenience, in one sentence;
- say what they may have experienced, in their terms (e.g. "your payment at checkout may
  have failed"), not ours;
- say the team is on it and when they can expect it fixed;
- tell them what to do meanwhile (retry later, reply to this email if a payment looks wrong).

Never include internal numbers, metric names, percentages, priorities, issue links, or any
technical or architectural language. Under 120 words. Sign off as "{FOUNDER_NAME}, Founder",
nothing else.

Call draft_email with the subject and body. If word_count comes back at 120 or more, tighten
it and call draft_email again. Finish with the subject and body exactly as drafted, and the
line "Nothing sends until you approve." No other commentary.""",
)

cto = Agent(
    name="CTO",
    model=CTO_MODEL,
    tools=[list_open_issues, score_priority_tool, create_issue],
    handoffs=[
        handoff(
            comms,
            input_type=CtoBrief,
            on_handoff=_noop,
            tool_description_override=(
                "Hand off to Comms once the issue is filed and customers noticed the problem."
            ),
        )
    ],
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
You are Rex, the CTO. You own engineering priority and the GitHub board.

The analyst has handed you a characterised drop. Work through these steps in order and
say what you found at each one, briefly:

1. Call list_open_issues. The `least_loaded` login takes this; say who and their count.
2. Score two axes, each 1-5:
   - behavioral_impact: how many users notice, and how badly.
   - structural_risk: 1 is a local patch; 5 means the fix crosses a core architectural
     boundary (payment session, auth, data integrity, a shared service contract).
   Call score_priority with both. State which axis drove the priority.
3. State the architectural reason in exactly one sentence: which boundary the fix
   touches, or why it is a local patch.
4. Call create_issue. Title: "[P<n>] <short title>". Body: the characterisation, both
   scores, the driver, and the architectural sentence. assignee = least_loaded.
   If create_issue fails, say so plainly and retry once.
5. If customers noticed (checkout, payments, login, anything user-facing), hand off to
   Comms, filling in every field of the handoff. Otherwise finish with the issue URL,
   the priority, the driver, and the architectural sentence.

Be terse. No preamble.""",
)

analyst = Agent(
    name="Analyst",
    model=ANALYST_MODEL,
    tools=[query_metrics],
    handoffs=[
        handoff(
            cto,
            input_type=Characterisation,
            on_handoff=_noop,
            tool_description_override=(
                "Hand off to the CTO when the drop looks like a system fault that needs engineering."
            ),
        ),
        handoff(
            comms,
            input_type=Characterisation,
            on_handoff=_noop,
            tool_description_override=(
                "Hand off to Comms when the drop is purely a customer-communication "
                "problem and no engineering fix is needed."
            ),
        ),
    ],
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
You are Ada, the analyst. You read metrics; you cannot change anything.

Given a flagged anomaly, call query_metrics on that metric. Characterise the drop in two
or three sentences: how far below baseline, whether it is a one-day collapse or a slide,
and whether the prior days were stable.

Then route it. This is a decision, not a sequence:
- System fault: a transactional metric (checkout, payments, login, API success) collapses
  abruptly, roughly 20% or more, against a stable baseline. Hand off to the CTO.
- Purely a customer-communication problem: customers are affected by something already
  known and planned, and no engineering fix is needed. Hand off to Comms.
- Ordinary variance: the move is within the day-to-day movement of the series, roughly
  under 15% with no other signal. Do NOT hand off. Reply with exactly one line:
  "Ordinary variance — <reason>."

Do not escalate noise. An engineer interrupted for a normal dip is the failure you exist
to prevent.""",
)


def anomaly_prompt(anomaly: dict) -> str:
    return (
        "Anomaly flagged by the deterministic check.\n"
        f"metric: {anomaly['metric']}\n"
        f"baseline (prior 7 days): {anomaly['baseline']}\n"
        f"current (last 24h): {anomaly['current']}\n"
        f"change: {anomaly['pct_change']}%\n"
        f"severity: {anomaly['severity']}\n"
        "Characterise it and decide where it goes."
    )


def run_chain(anomaly: dict, hooks: RunHooks | None = None):
    """Start at the analyst and let the handoffs decide the rest."""
    return Runner.run_sync(analyst, anomaly_prompt(anomaly), hooks=hooks, max_turns=MAX_TURNS)


def tool_outputs(result, tool_name: str) -> list:
    """Every return value `tool_name` produced during the run, in order.

    This is how app.py gets the draft to put behind the Approve button without
    parsing the model's prose.
    """
    call_ids = {
        item.raw_item.call_id
        for item in result.new_items
        if isinstance(item, ToolCallItem) and getattr(item.raw_item, "name", None) == tool_name
    }
    return [
        item.output
        for item in result.new_items
        if isinstance(item, ToolCallOutputItem) and item.raw_item["call_id"] in call_ids
    ]


class PrintHooks(RunHooks):
    """Every step to stdout. app.py replaces this with hooks that post to the thread."""

    async def on_agent_start(self, context, agent):
        print(f"\n▶ {agent.name} started")

    async def on_llm_end(self, context, agent, response):
        for item in response.output:
            if item.type == "message":
                for part in item.content:
                    if getattr(part, "text", None):
                        print(f"  {agent.name}: {part.text}")
            elif item.type == "function_call" and item.name.startswith("transfer_to_"):
                for k, v in json.loads(item.arguments).items():
                    print(f"  {agent.name}: [{k}] {v}")
            elif item.type == "function_call":
                print(f"  ⚙ {agent.name} → {item.name}({item.arguments})")

    async def on_tool_end(self, context, agent, tool, result):
        print(f"  ✓ {tool.name} → {result}")

    async def on_handoff(self, context, from_agent, to_agent):
        print(f"  ↪ {from_agent.name} → {to_agent.name}")

    async def on_agent_end(self, context, agent, output):
        print(f"■ {agent.name} done")


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv(".env")
    load_dotenv(".env.local")

    branch = sys.argv[1] if len(sys.argv) > 1 else "real"
    metric = {"real": "checkout_success", "noise": "signups"}[branch]
    # Straight from window_deltas, bypassing the 20% threshold on purpose: the
    # noise branch exists to prove the analyst stops even when handed a dip.
    delta = next(d for d in db.window_deltas() if d["metric"] == metric)
    anomaly = {**delta, "severity": db.severity_for(delta["pct_change"])}
    print(f"claimed anomaly: {anomaly}")

    result = run_chain(anomaly, hooks=PrintHooks())

    print(f"\n=== final ({result.last_agent.name}) ===")
    print(result.final_output)
    for out in tool_outputs(result, "create_issue"):
        print(f"issue: {out['url']}")
    for out in tool_outputs(result, "draft_email"):
        print(f"draft: {out['subject']!r} ({out['word_count']} words)")
