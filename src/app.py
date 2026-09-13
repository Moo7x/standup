"""Slack surface: handlers, hooks, poller. The only file that imports Slack."""

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone

import requests
from agents import RunHooks
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src import db
from src.agents import run_chain, tool_outputs

load_dotenv()              # .env
load_dotenv(".env.local")  # fallback; never overrides values already set

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
OPS_CHANNEL = "#ops"
POLL_SECONDS = 15

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    sys.exit("SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set in .env or .env.local")
if not os.environ.get("DATABASE_URL"):
    sys.exit("DATABASE_URL must be set in .env or .env.local")

# Must run before App() — Bolt copies the root logger's level at construction.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("standup")

# Socket Mode: Slack dials out to us, so there is no inbound HTTP request to
# verify and no signing secret is needed.
app = App(token=SLACK_BOT_TOKEN, request_verification_enabled=False)

# One Slack app, three identities via chat:write.customize. Keyed by Agent.name.
IDENTITY = {
    "Analyst": {"username": "Ada · Analyst", "icon_emoji": ":bar_chart:"},
    "CTO": {"username": "Rex · CTO", "icon_emoji": ":hammer_and_wrench:"},
    "Comms": {"username": "Mia · Comms", "icon_emoji": ":envelope:"},
}

# Set by /seed-anomaly so the poller runs now instead of on its next tick.
wake = threading.Event()


# --- The chain, narrated into one thread --------------------------------------

class SlackHooks(RunHooks):
    """Every step of a run becomes a reply in the anomaly's thread, under the
    identity of the agent that took it. The status lines are what fills the
    gap while the model thinks; without them the thread looks frozen."""

    def __init__(self, client, channel, thread_ts, anomaly):
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.anomaly = anomaly

    def say(self, agent_name, text):
        self.client.chat_postMessage(
            channel=self.channel, thread_ts=self.thread_ts, text=text, **IDENTITY[agent_name]
        )
        log.info("%s: %s", agent_name, text.replace("\n", " / ")[:160])

    async def on_agent_start(self, context, agent):
        status = {
            "Analyst": f"_pulling the last 7 days of `{self.anomaly['metric']}`…_",
            "CTO": "_pulling open issues to see who's least loaded…_",
            "Comms": "_drafting a customer note — nothing sends until you approve…_",
        }[agent.name]
        self.say(agent.name, status)

    async def on_tool_start(self, context, agent, tool):
        self.say(agent.name, f"_using `{tool.name}`…_")

    async def on_tool_end(self, context, agent, tool, result):
        line = render_tool_result(tool.name, result)
        if line:
            self.say(agent.name, line)

    async def on_llm_end(self, context, agent, response):
        # An agent that hands off never reaches on_agent_end, so its real
        # output is the typed handoff payload. Post that as the agent's words.
        for item in response.output:
            if item.type == "function_call" and item.name.startswith("transfer_to_"):
                self.say(agent.name, render_handoff(json.loads(item.arguments)))

    async def on_agent_end(self, context, agent, output):
        self.say(agent.name, str(output))


def render_tool_result(tool_name: str, r) -> str | None:
    """One line per tool result, so the thread shows what each agent saw."""
    if not isinstance(r, dict):
        return None
    if tool_name == "query_metrics":
        return f"_{r['metric']}: baseline {r['baseline']} → {r['current']} in the last 24h ({r['pct_change']:+}%)_"
    if tool_name == "list_open_issues":
        load = ", ".join(f"{k} {v}" for k, v in sorted(r["open_count_by_assignee"].items()))
        return f"_open issues: {load} — least loaded: {r['least_loaded']}_"
    if tool_name == "score_priority":
        return f"_{r['priority']} — `{r['driver']}` drove it_"
    if tool_name == "create_issue":
        return f"_filed {r['url']} → {r['assignee']}_"
    if tool_name == "draft_email":
        return f"_draft ready, {r['word_count']} words_"
    return None


def render_handoff(payload: dict) -> str:
    if "characterisation" in payload:                      # Analyst → CTO / Comms
        return payload["characterisation"]
    return (                                               # CTO → Comms
        f"*{payload['priority']}* — driven by `{payload['driver']}`.\n"
        f"{payload['architectural_reason']}\n"
        f"Assigned to {payload['assignee']}. Issue: {payload['issue_url']}"
    )


def run_anomaly(row):
    """Open the thread, then let the agents fill it."""
    metric, base, cur, pct = row["metric"], row["baseline"], row["current"], row["pct_change"]
    root = app.client.chat_postMessage(
        channel=OPS_CHANNEL,
        text=f"*{metric}* dropped {abs(pct)}% in the last 24h — {base} → {cur}. Looking into it.",
        **IDENTITY["Analyst"],
    )
    hooks = SlackHooks(app.client, root["channel"], root["ts"], row)
    try:
        result = run_chain(row, hooks=hooks)
    except Exception as e:
        log.exception("chain failed for anomaly #%s", row["id"])
        hooks.say("Analyst", f"_chain failed: {type(e).__name__}: {e}_")
        return None
    log.info("anomaly #%s finished at %s", row["id"], result.last_agent.name)

    drafts = tool_outputs(result, "draft_email")
    if drafts:
        app.client.chat_postMessage(
            channel=root["channel"], thread_ts=root["ts"],
            text="Send this to customers?", blocks=approval_blocks(drafts[-1]),
            **IDENTITY["Comms"],
        )
    return result


# --- Approve / Cancel: the one human touch, on the one irreversible action ------

def approval_blocks(draft: dict) -> list:
    """The draft rides along in the button value, so a click carries everything
    needed to send. No draft table; the thread is the record."""
    value = json.dumps({"subject": draft["subject"], "body": draft["body"]})
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Send this to customers?*"}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": "approve_email", "style": "primary",
             "text": {"type": "plain_text", "text": "Approve — send"}, "value": value},
            {"type": "button", "action_id": "cancel_email", "style": "danger",
             "text": {"type": "plain_text", "text": "Cancel"}, "value": value},
        ]},
    ]


def brevo_send(subject: str, body: str, to: str) -> str:
    """The only function in the codebase that sends email. Returns Brevo's message id."""
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": os.environ["BREVO_API_KEY"], "accept": "application/json"},
        json={
            "sender": {"name": os.environ["BREVO_SENDER_NAME"], "email": os.environ["BREVO_SENDER_EMAIL"]},
            "to": [{"email": to}],
            "subject": subject,
            "textContent": body,
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("messageId", "?")


_approved: set[str] = set()   # message ts already sent; a double-click must not send twice


def replace_buttons(client, channel, ts, text):
    """Swap the Approve/Cancel message for its outcome. No click leaves buttons behind."""
    client.chat_update(channel=channel, ts=ts, text=text,
                       blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}])


@app.action("approve_email")
def approve_email(ack, body, client, logger):
    ack()
    channel, ts, user = body["channel"]["id"], body["message"]["ts"], body["user"]["id"]
    draft = json.loads(body["actions"][0]["value"])
    to = os.environ.get("CUSTOMER_EMAIL")
    if not to:
        replace_buttons(client, channel, ts, "⚠️ *Not sent* — `CUSTOMER_EMAIL` is not set. Set it, restart, re-run.")
        return
    if ts in _approved:
        return
    _approved.add(ts)
    try:
        message_id = brevo_send(draft["subject"], draft["body"], to)
    except Exception as e:
        logger.exception("brevo send failed")
        replace_buttons(client, channel, ts, f"⚠️ *Send failed* — {type(e).__name__}: {e}. Nothing sent; re-run for a fresh draft.")
        return
    when = datetime.now(timezone.utc).strftime("%H:%M UTC")
    replace_buttons(client, channel, ts, f"✅ *Sent* to {to} — approved by <@{user}> at {when}. Brevo id `{message_id}`.")
    logger.info("email sent to %s by %s: %s", to, user, message_id)


@app.action("cancel_email")
def cancel_email(ack, body, client, logger):
    ack()
    user = body["user"]["id"]
    replace_buttons(client, body["channel"]["id"], body["message"]["ts"], f"❌ *Cancelled* by <@{user}>. Nothing sent.")
    logger.info("email cancelled by %s", user)


# --- Slack handlers -----------------------------------------------------------

@app.command("/seed-anomaly")
def seed_anomaly(ack, command, respond):
    """`/seed-anomaly real`  runs the deterministic check; the seeded 34%
    checkout_success drop is what it finds.
    `/seed-anomaly noise` force-inserts the 8% signups dip, which sits below
    the threshold, so the analyst gets something to dismiss."""
    ack()
    kind = (command.get("text") or "real").strip().lower()
    if kind == "real":
        rows = db.detect_anomalies()
    elif kind == "noise":
        sig = next((d for d in db.window_deltas() if d["metric"] == "signups"), None)
        rows = [db.insert_anomaly(
            sig["metric"], sig["baseline"], sig["current"], sig["pct_change"], "low",
        )] if sig else []
    else:
        respond("Usage: `/seed-anomaly [real|noise]`")
        return

    if not rows:
        respond(f"Nothing inserted for `{kind}` — no drop past threshold, or one is already pending.")
        return
    lines = [f"#{r['id']} `{r['metric']}` {r['pct_change']}% ({r['severity']})" for r in rows]
    respond("Inserted " + ", ".join(lines) + ". Starting the chain.")
    wake.set()


# --- Poller -------------------------------------------------------------------

def poll_anomalies():
    """Claim one pending anomaly per tick and run the chain on it. processed_at
    is stamped in the claim itself, so a row can never be handed out twice."""
    while True:
        try:
            row = db.claim_next_anomaly()
            if row:
                log.info("claimed anomaly #%s %s pct=%s", row["id"], row["metric"], row["pct_change"])
                run_anomaly(row)
                continue  # drain the queue before sleeping
        except Exception:
            log.exception("poller tick failed")
        wake.wait(POLL_SECONDS)
        wake.clear()


def main():
    threading.Thread(target=poll_anomalies, name="poller", daemon=True).start()
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
