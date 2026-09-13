"""Postgres reads and the deterministic anomaly check. Imports nothing above it."""

import os
from typing import Protocol

import psycopg
from psycopg.rows import dict_row

# Drops at or beyond this fraction of the prior-7-day baseline are anomalies.
DROP_THRESHOLD = 0.20


def connect():
    # One short-lived connection per call. Neon handles that fine at demo
    # volume, and it keeps the module free of pool lifecycle.
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


# --- product.metrics: the startup's data --------------------------------------

class MetricsSource(Protocol):
    """The one seam. A PostHogMetricsSource drops in here without touching
    agents or tools."""

    def query_metrics(self, metric: str, days: int) -> list[dict]: ...


class PostgresMetricsSource:
    def query_metrics(self, metric: str, days: int) -> list[dict]:
        with connect() as conn:
            return conn.execute(
                """
                select metric, value, recorded_at
                from product.metrics
                where metric = %s
                and recorded_at >= now() - make_interval(days => %s)
                order by recorded_at
                """,
                (metric, days),
            ).fetchall()


metrics: MetricsSource = PostgresMetricsSource()


def query_metrics(metric: str, days: int) -> list[dict]:
    return metrics.query_metrics(metric, days)


# --- ops.anomalies: the system's own state ------------------------------------

def window_deltas() -> list[dict]:
    """Per metric: last-24h average vs the prior 7-day average.

    Deterministic SQL, no model involved. Metrics missing either window are
    skipped rather than reported with a null baseline.
    """
    with connect() as conn:
        return conn.execute(
            """
            select metric,
                round(baseline, 2)                                    as baseline,
                   round("current", 2)                                   as "current",
                   round(("current" - baseline) / baseline * 100, 1)     as pct_change
            from (
                select metric,
                       avg(value) filter (where recorded_at >= now() - interval '24 hours')
                           as "current",
                       avg(value) filter (where recorded_at <  now() - interval '24 hours'
                                          and recorded_at >= now() - interval '8 days')
                           as baseline
                from product.metrics
                group by metric
            ) w
            where baseline is not null and "current" is not null and baseline <> 0
            order by metric
            """
        ).fetchall()


def severity_for(pct_change: float) -> str:
    drop = -float(pct_change)
    if drop >= 30:
        return "high"
    if drop >= DROP_THRESHOLD * 100:
        return "medium"
    return "low"


def insert_anomaly(metric, baseline, current, pct_change, severity) -> dict:
    with connect() as conn:
        return conn.execute(
            """
            insert into ops.anomalies (metric, baseline, "current", pct_change, severity)
            values (%s, %s, %s, %s, %s)
            returning *
            """,
            (metric, baseline, current, pct_change, severity),
        ).fetchone()


def detect_anomalies() -> list[dict]:
    """Flag drops over the threshold and record them. Returns the rows inserted.

    A metric that already has an unprocessed anomaly is skipped, so calling
    this repeatedly never stacks duplicate work for the poller.
    """
    with connect() as conn:
        pending = {
            r["metric"]
            for r in conn.execute(
                "select metric from ops.anomalies where processed_at is null"
            )
        }
    inserted = []
    for d in window_deltas():
        if d["pct_change"] > -DROP_THRESHOLD * 100 or d["metric"] in pending:
            continue
        inserted.append(
            insert_anomaly(
                d["metric"], d["baseline"], d["current"], d["pct_change"],
                severity_for(d["pct_change"]),
            )
        )
    return inserted


def claim_next_anomaly() -> dict | None:
    """Atomically take the oldest pending anomaly. None if the queue is empty.

    Stamping processed_at in the same statement that selects the row is what
    makes the poller idempotent: no row is ever handed out twice, even with
    two pollers running.
    """
    with connect() as conn:
        return conn.execute(
            """
            update ops.anomalies
            set processed_at = now()
            where id = (
                select id from ops.anomalies
                where processed_at is null
                order by detected_at
                limit 1
                for update skip locked
            )
            returning *
            """
        ).fetchone()
