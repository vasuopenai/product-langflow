"""Anonymous app usage: how many installs use the app and what they do, per day.

Counts per (day, install ID, action) in mobile.usage. The install ID is the random X-Client-Id the
app creates on first launch (no name, email or device ID). Only requests that carry it are
counted, so tests with curl don't inflate the numbers. Days are UTC.
"""

import hashlib

SCHEMA = [
    "CREATE SCHEMA IF NOT EXISTS mobile",
    """CREATE TABLE IF NOT EXISTS mobile.usage (
        day DATE NOT NULL, client_id TEXT NOT NULL, action TEXT NOT NULL, n INTEGER NOT NULL,
        PRIMARY KEY (day, client_id, action))""",
]
# What each app endpoint counts as. Polling a scan's progress isn't an action.
ACTIONS = {("POST", "/api/ask"): "ask", ("POST", "/api/web"): "web", ("POST", "/api/scan"): "scan",
           ("GET", "/api/nearby"): "store_lookup"}
PREFIXES = {("GET", "/api/products/"): "product_view"}
ACTION_ORDER = ["ask", "scan", "product_view", "store_lookup", "web"]


def label(client_id):
    """A short, stable label for an install (a hash, so the raw ID isn't shown)."""
    return hashlib.sha1(client_id.encode()).hexdigest()[:8]


def init_schema(conn):
    for sql in SCHEMA:
        conn.execute(sql)
    conn.commit()


def action_for(method, path):
    if (method, path) in ACTIONS:
        return ACTIONS[(method, path)]
    return next((a for (m, prefix), a in PREFIXES.items() if m == method and path.startswith(prefix)), None)


def record(conn, client_id, action):
    conn.execute("INSERT INTO mobile.usage VALUES (CURRENT_DATE, %s, %s, 1) "
                 "ON CONFLICT (day, client_id, action) DO UPDATE SET n = mobile.usage.n + 1", (client_id, action))
    conn.commit()


def summary(conn, days=30):
    """Active installs, new installs, actions per day, return rate, and per-install totals."""
    def scalar(sql, *params):
        return conn.execute(sql, params).fetchone()[0]

    active = {f"{d}d": scalar("SELECT count(DISTINCT client_id) FROM mobile.usage WHERE day > CURRENT_DATE - %s", d)
              for d in (1, 7, 30)}
    total = scalar("SELECT count(DISTINCT client_id) FROM mobile.usage")
    returning = scalar("""SELECT count(*) FROM (SELECT client_id FROM mobile.usage GROUP BY client_id
                          HAVING count(DISTINCT day) >= 2) t""")
    first_days = """SELECT client_id, min(day) AS first_day FROM mobile.usage GROUP BY client_id"""
    daily = {}
    for day, n in conn.execute("""SELECT day, count(DISTINCT client_id) FROM mobile.usage
                                  WHERE day > CURRENT_DATE - %s GROUP BY day""", (days,)):
        daily.setdefault(day.isoformat(), {})["active"] = n
    for day, n in conn.execute(f"""SELECT first_day, count(*) FROM ({first_days}) f
                                   WHERE first_day > CURRENT_DATE - %s GROUP BY first_day""", (days,)):
        daily.setdefault(day.isoformat(), {})["new"] = n
    for day, action, n in conn.execute("""SELECT day, action, sum(n) FROM mobile.usage
                                          WHERE day > CURRENT_DATE - %s GROUP BY day, action""", (days,)):
        daily.setdefault(day.isoformat(), {})[action] = int(n)
    installs = [
        {"install": label(cid), "first_seen": first.isoformat(), "last_seen": last.isoformat(), "days_active": active_days,
         **{a: int(totals.get(a, 0)) for a in ACTION_ORDER}}
        for cid, first, last, active_days, totals in conn.execute(
            """SELECT client_id, min(day), max(day), count(DISTINCT day),
                      (SELECT jsonb_object_agg(action, s) FROM (SELECT action, sum(n) s FROM mobile.usage u2
                       WHERE u2.client_id = u.client_id GROUP BY action) a)
               FROM mobile.usage u GROUP BY client_id ORDER BY max(day) DESC, count(DISTINCT day) DESC LIMIT 500""")]
    actions_total = {a: int(n) for a, n in conn.execute("SELECT action, sum(n) FROM mobile.usage GROUP BY action")}
    return {"active": active, "total_installs": total, "returning_installs": returning,
            "actions_total": actions_total, "daily": [{"day": d, **v} for d, v in sorted(daily.items())],
            "installs": installs, "days": days, "timezone": "UTC"}
