"""Read and write queries. All timestamps are naive local time strings."""

import sqlite3
from typing import Any


def _range_clause(start: str | None, end: str | None) -> tuple[str, tuple]:
	"""Build a `AND timestamp >= ? AND timestamp < ?`-style fragment for the given
	optional bounds. Returns (sql_fragment, params) — sql_fragment is "" if both
	bounds are None. `end` is treated as exclusive (see edge cases below)."""
	clauses = []
	params: list[str] = []
	if start:
		clauses.append("timestamp >= ?")
		params.append(start)
	if end:
		clauses.append("timestamp < ?")
		params.append(end)
	if not clauses:
		return "", ()
	return " AND " + " AND ".join(clauses), tuple(params)


def get_machines(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id, name, floor, has_telemetry FROM machines ORDER BY id")
    return [dict(r) | {"has_telemetry": bool(r["has_telemetry"])} for r in rows]


def get_machine(conn: sqlite3.Connection, machine_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, name, floor, has_telemetry FROM machines WHERE id = ?", (machine_id,)
    ).fetchone()
    if row is None:
        return None
    return dict(row) | {"has_telemetry": bool(row["has_telemetry"])}


def get_drink_types(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id, name, label FROM drink_types ORDER BY id")
    return [dict(r) for r in rows]


def drink_type_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM drink_types WHERE name = ?", (name,)).fetchone()
    return row is not None


def insert_brew(
    conn: sqlite3.Connection,
    machine_id: int,
    drink_type: str,
    timestamp: str,
    duration_s: float,
    temp_c: float,
    source: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO brew_events (machine_id, drink_type, timestamp, duration_s, temp_c, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (machine_id, drink_type, timestamp, duration_s, temp_c, source),
    )
    return cur.lastrowid


def insert_maintenance(
    conn: sqlite3.Connection,
    machine_id: int,
    type: str,
    timestamp: str,
    note: str | None = None,
    error_code: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO maintenance_events (machine_id, type, timestamp, note, error_code)
        VALUES (?, ?, ?, ?, ?)
        """,
        (machine_id, type, timestamp, note, error_code),
    )
    return cur.lastrowid


def get_stats(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """Dashboard numbers: totals, per-drink, per-day."""
    range_clause, range_params = _range_clause(start, end)

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM brew_events WHERE 1=1{range_clause}",
        range_params
    ).fetchone()["n"]

    per_drink = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT dt.name, dt.label, COUNT(be.id) AS count
            FROM drink_types dt
            LEFT JOIN brew_events be ON be.drink_type = dt.name{range_clause}
            GROUP BY dt.id
            ORDER BY dt.id
            """,
            range_params
        )
    ]

    per_day = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT DATE(timestamp) AS day, COUNT(*) AS count
            FROM brew_events
            WHERE 1=1{range_clause}
            GROUP BY DATE(timestamp)
            ORDER BY day
            """,
            range_params
        )
    ]
    return {"total_brews": total, "per_drink": per_drink, "per_day": per_day}


def get_machine_health(conn: sqlite3.Connection, machine_id: int, start: str | None = None, end: str | None = None) -> dict[str, Any] | None:
    """Machine card: brew activity plus maintenance history."""
    machine = get_machine(conn, machine_id)
    if machine is None:
        return None
    range_clause, range_params = _range_clause(start, end)

    brews = conn.execute(
        f"""
        SELECT COUNT(*) AS count, MAX(timestamp) AS last_brew
        FROM brew_events WHERE machine_id = ?{range_clause}
        """,
        (machine_id,) + range_params,
    ).fetchone()
    last_maintenance = conn.execute(
        f"""
        SELECT type, timestamp, note, error_code
        FROM maintenance_events
        WHERE machine_id = ? AND type != 'error'{range_clause}
        ORDER BY timestamp DESC LIMIT 1
        """,
        (machine_id,) + range_params,
    ).fetchone()
    recent_errors = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT timestamp, error_code, note
            FROM maintenance_events
            WHERE machine_id = ? AND type = 'error'{range_clause}
            ORDER BY timestamp DESC LIMIT 5
            """,
            (machine_id,) + range_params,
        )
    ]
    specialty = conn.execute(
        f"""
        SELECT dt.label, COUNT(*) AS n
        FROM brew_events be
        JOIN drink_types dt ON dt.name = be.drink_type
        WHERE be.machine_id = ?{range_clause}
        GROUP BY dt.id
        ORDER BY n DESC, dt.name
        LIMIT 1
        """,
        (machine_id,) + range_params,
    ).fetchone()
    return machine | {
        "brew_count": brews["count"],
        "last_brew": brews["last_brew"],
        "last_maintenance": dict(last_maintenance) if last_maintenance else None,
        "recent_errors": recent_errors,
        "specialty": specialty["label"] if specialty else None,
    }
