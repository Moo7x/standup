"""Schema + fixture data. Re-runnable: truncates and reloads every time.

    python seed.py
"""

from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.local")

from src.db import connect  # noqa: E402  (needs DATABASE_URL loaded first)

DDL = """
create schema if not exists product;   -- the startup's data; the analyst reads it
create schema if not exists ops;       -- the system's own state

create table if not exists product.metrics (
    id          bigserial primary key,
    metric      text        not null,
    value       numeric     not null,
    recorded_at timestamptz not null
);
create index if not exists metrics_metric_recorded_at
    on product.metrics (metric, recorded_at desc);

create table if not exists ops.anomalies (
    id           bigserial primary key,
    metric       text        not null,
    baseline     numeric     not null,
    "current"    numeric     not null,
    pct_change   numeric     not null,
    severity     text        not null check (severity in ('low', 'medium', 'high')),
    detected_at  timestamptz not null default now(),
    processed_at timestamptz
);
create index if not exists anomalies_pending
    on ops.anomalies (detected_at) where processed_at is null;

truncate product.metrics, ops.anomalies restart identity;
"""

# 14 daily values per metric, oldest first. Index 13 is "today" and is the
# only row inside the detector's 24h window; indexes 6..12 are its 7-day
# baseline, and their jitter sums to zero so the baseline is exactly BASELINE.
#
#   signups           ~200/day, today 184   -> -8%,  below threshold (noise)
#   checkout_success  ~92.0% ,  today 60.7  -> -34%, flagged (real)
SERIES = {
    "signups": (
        200,
        [+3, -2, +5, -1, 0, +2,  +2, -3, +1, +4, -2, -1, -1],
        184,
    ),
    "checkout_success": (
        92.0,
        [+0.4, -0.3, +0.6, -0.2, 0.0, +0.3,  +0.5, -0.4, +0.2, +0.3, -0.6, +0.1, -0.1],
        60.7,
    ),
}


def rows():
    for metric, (baseline, jitter, today) in SERIES.items():
        values = [baseline + j for j in jitter] + [today]
        for i, value in enumerate(values):
            # A one-minute offset keeps "yesterday" strictly older than
            # now() - 24h when the detector runs seconds after seeding.
            yield metric, value, 13 - i


def main():
    with connect() as conn:
        conn.execute(DDL)
        conn.cursor().executemany(
            """
            insert into product.metrics (metric, value, recorded_at)
            values (%s, %s, now() - make_interval(days => %s, mins => 1))
            """,
            list(rows()),
        )
        counts = conn.execute(
            "select metric, count(*) as n from product.metrics group by metric order by metric"
        ).fetchall()
    for c in counts:
        print(f"{c['metric']:<18} {c['n']} rows")
    print("ops.anomalies       0 rows")


if __name__ == "__main__":
    main()
