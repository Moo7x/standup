"""Slack surface: handlers, buttons, poller. The only file that imports Slack."""

import logging
import os
import sys
import threading
import time

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src import db

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
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("standup")

# Socket Mode: Slack dials out to us, so there is no inbound HTTP request to
# verify and no signing secret is needed.
app = App(token=SLACK_BOT_TOKEN, request_verification_enabled=False)

# One Slack app, three identities via chat:write.customize.
ANALYST = {"username": "Ada · Analyst", "icon_emoji": ":bar_chart:"}
CTO = {"username": "Rex · CTO", "icon_emoji": ":hammer_and_wrench:"}
COMMS = {"username": "Mia · Comms", "icon_emoji": ":envelope:"}


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
    respond("Inserted " + ", ".join(lines) + f". Poller picks it up within {POLL_SECONDS}s.")


def poll_anomalies():
    """Claim one pending anomaly per tick. processed_at is stamped in the
    claim itself, so a row can never be handed out twice."""
    while True:
        try:
            row = db.claim_next_anomaly()
            if row:
                log.info(
                    "claimed anomaly #%s %s baseline=%s current=%s pct=%s severity=%s",
                    row["id"], row["metric"], row["baseline"], row["current"],
                    row["pct_change"], row["severity"],
                )
        except Exception:
            log.exception("poller tick failed")
        time.sleep(POLL_SECONDS)


def main():
    threading.Thread(target=poll_anomalies, name="poller", daemon=True).start()
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
