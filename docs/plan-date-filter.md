# Implementation plan: dashboard date-range filter (ticket 005)

This document assumes you have zero prior context on BrewOps. It tells you exactly
which files and functions to change, what to add, and how to verify it. Read the
whole thing before touching any file — later sections explain edge cases that affect
earlier steps.

## Background you need

- BrewOps is a FastAPI app (`src/brewops/api/main.py`) backed by SQLite, with a
  plain-JS frontend (no build step) in `src/brewops/frontend/`.
- Two tables carry a `timestamp` column in the format `'YYYY-MM-DD HH:MM:SS'`
  (naive, no timezone): `brew_events` and `maintenance_events`
  (`src/brewops/db/schema.py`).
- The dashboard currently has **no date filtering anywhere** — every query reads the
  entire table. That's exactly the bug in `tickets/005-date-filter.md`.
- All SQL lives in `src/brewops/db/queries.py` as plain strings passed to
  `conn.execute(...)`. There is no query-builder / ORM. You will be editing raw SQL
  strings directly.
- Run the app with `uv run start`, open `http://localhost:8123`. Run tests with
  `uv run pytest`.

## Goal

Add a "from" / "to" date-range control to the dashboard. When set, every number and
chart on the dashboard (stat tiles, per-drink bars, timeline, and each machine card
including its "specialty" drink) reflects only brews (and, for machine cards,
maintenance/errors) inside that range. When no range is set, behavior is unchanged
(show everything — same as today).

---

## Step 1 — Backend: query layer (`src/brewops/db/queries.py`)

Add a small shared helper near the top of the file (after the imports, before
`get_machines`):

```python
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
```

Then change these two functions to accept `start: str | None = None, end: str | None
= None` and splice the fragment into each of their queries:

### `get_stats(conn, start=None, end=None)` — lines ~68-94 today

All three sub-queries need `WHERE 1=1 {clause}` (or append to an existing WHERE):

- `total`: `SELECT COUNT(*) AS n FROM brew_events WHERE 1=1{clause}`, params = the
  range params.
- `per_drink`: add `AND (be.timestamp IS NULL OR ...)` — careful, this is a LEFT
  JOIN, so filtering must go in the **JOIN condition**, not a WHERE clause, or you'll
  drop drink types with zero brews in range. Change:
  ```sql
  LEFT JOIN brew_events be ON be.drink_type = dt.name
  ```
  to:
  ```sql
  LEFT JOIN brew_events be ON be.drink_type = dt.name{clause}
  ```
  (the clause here filters `be.timestamp`, and because it's in the ON clause, drink
  types with no matching rows still appear with count 0 — this preserves today's
  behavior of listing every drink type even at 0).
- `per_day`: `SELECT DATE(timestamp) ... FROM brew_events WHERE 1=1{clause} GROUP BY
  ...`.

Use the same `start`/`end` params tuple for each `conn.execute(sql, params)` call
(remember: `per_drink`'s params go into the JOIN's positional `?`, which still works
fine with sqlite's positional binding as long as the params tuple matches).

### `get_machine_health(conn, machine_id, start=None, end=None)` — lines ~97-148 today

Apply the same `_range_clause` to the four sub-queries, appended after the existing
`machine_id` condition (params order: `machine_id` first, then range params):

- `brews` (count + last_brew): `WHERE machine_id = ?{clause}`
- `last_maintenance`: `WHERE machine_id = ? AND type != 'error'{clause}` — see edge
  case note below on whether maintenance should share the same range.
- `recent_errors`: `WHERE machine_id = ? AND type = 'error'{clause} ORDER BY
  timestamp DESC LIMIT 5` — the `LIMIT 5` stays; a date range just narrows the pool
  it's chosen from. **Do not remove the LIMIT.**
- `specialty`: `WHERE be.machine_id = ?{clause} GROUP BY dt.id ORDER BY n DESC,
  dt.name LIMIT 1` — this is the "most-brewed drink" the ticket specifically
  mentions ("since the beginning of time").

Decision needed (pick one, both are reasonable — recommend option A for simplicity):

- **A (recommended):** the same `start`/`end` range applies to both brews and
  maintenance/errors on a machine card. One filter, one mental model.
- **B:** only brews are filtered; maintenance/errors stay unbounded. Simpler to
  argue "last maintenance" should always show the true last event regardless of the
  viewing window.

This plan assumes **A**. If you pick B, only apply `_range_clause` to the `brews`
and `specialty` queries, not `last_maintenance`/`recent_errors`.

---

## Step 2 — Backend: API layer (`src/brewops/api/main.py`)

Add two optional query parameters to the two GET routes that back the dashboard.
Use plain `str | None` query params (not `datetime`) so you can reuse the existing
naive-local-time string format and validate them yourself — FastAPI's automatic
`datetime` parsing would require timezone-aware ISO strings, which conflicts with
this codebase's naive-local convention.

```python
from fastapi import Query
```

### New helper: parse and validate range bounds

Add near `parse_timestamp` (which stays as-is, unchanged — it's for single-value
POST bodies and has different rules, e.g. it rejects future timestamps, which a
filter's `end` must NOT do):

```python
def parse_range_bound(value: str | None, *, param_name: str) -> str | None:
    """Parse an optional `from`/`to` query param into the storage timestamp format.
    Unlike parse_timestamp, does not reject future values (a filter end date in the
    future is meaningless but harmless — it just returns no extra rows)."""
    if value is None or value == "":
        return None
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise HTTPException(400, f"unparsable {param_name} {value!r}")
```

### Route changes

```python
@app.get("/api/stats")
def stats(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_db),
):
    start = parse_range_bound(from_, param_name="from")
    end = parse_range_bound(to, param_name="to")
    if start and end and start > end:
        raise HTTPException(400, "'from' must be before 'to'")
    return queries.get_stats(conn, start=start, end=end)
```

```python
@app.get("/api/machines/{machine_id}")
def machine_health(
    machine_id: int,
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_db),
):
    start = parse_range_bound(from_, param_name="from")
    end = parse_range_bound(to, param_name="to")
    if start and end and start > end:
        raise HTTPException(400, "'from' must be before 'to'")
    health = queries.get_machine_health(conn, machine_id, start=start, end=end)
    if health is None:
        raise HTTPException(404, f"no machine with id {machine_id}")
    return health
```

Note the param is named `from` in the URL (a JS-friendly name) but must be aliased
to `from_` in Python since `from` is a reserved keyword — the `Query(None,
alias="from")` handles that.

Query params arrive as `YYYY-MM-DDTHH:MM` (matching what a `datetime-local` input
produces) — `TIMESTAMP_FORMATS` already includes that format, so no change needed
there.

---

## Step 3 — Frontend: controls (`src/brewops/frontend/index.html`)

Add a small filter bar as the first child of `<section id="dashboard">`, before the
`.stat-tiles` div:

```html
<div class="panel filter-bar">
  <label for="filter-from">From</label>
  <input type="datetime-local" id="filter-from">
  <label for="filter-to">To</label>
  <input type="datetime-local" id="filter-to">
  <button type="button" id="filter-apply">Apply</button>
  <button type="button" id="filter-clear">Clear</button>
  <p id="filter-message" class="message" role="status"></p>
</div>
```

Reuse the existing `.panel` / `.message` CSS classes already used elsewhere in
`index.html` — no new CSS classes are required, but check `src/brewops/frontend/style.css`
for whether `.filter-bar` needs a `display: flex` rule to lay the inputs out in a
row (existing `.panel` is likely block-level / full width).

---

## Step 4 — Frontend: logic (`src/brewops/frontend/app.js`)

### Track current range in module state

Near the top of the file, after `fetchJSON`, add:

```js
let currentRange = { from: "", to: "" };
```

### Build a query string helper

```js
function rangeQuery() {
  const params = new URLSearchParams();
  if (currentRange.from) params.set("from", currentRange.from);
  if (currentRange.to) params.set("to", currentRange.to);
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}
```

### Update `loadDashboard()` (currently lines 77-89) to use it

```js
async function loadDashboard() {
  const qs = rangeQuery();
  const stats = await fetchJSON(`/api/stats${qs}`);
  document.getElementById("total-brews").textContent = stats.total_brews;
  const lastDay = stats.per_day[stats.per_day.length - 1];
  document.getElementById("brews-today").textContent = lastDay ? lastDay.count : 0;
  renderDrinkBars(stats.per_drink);
  renderTimeline(stats.per_day);

  const machines = await fetchJSON("/api/machines"); // unfiltered — reference data
  document.getElementById("machine-count").textContent = machines.length;
  const healths = await Promise.all(
    machines.map((m) => fetchJSON(`/api/machines/${m.id}${qs}`))
  );
  renderMachineCards(healths);
}
```

Do NOT add the query string to `/api/machines` (the plain list) — that endpoint
returns static reference data (id/name/floor/has_telemetry), not anything
timestamp-dependent, and doesn't accept the params.

### Wire up the filter controls

Add to `setupForms()` (or a new `setupFilterBar()` function called alongside it at
the bottom of the file):

```js
function setupFilterBar() {
  const applyBtn = document.getElementById("filter-apply");
  const clearBtn = document.getElementById("filter-clear");
  const message = document.getElementById("filter-message");

  applyBtn.addEventListener("click", async () => {
    const from = document.getElementById("filter-from").value;
    const to = document.getElementById("filter-to").value;
    message.textContent = "";
    message.className = "message";
    if (from && to && from > to) {
      message.textContent = "'From' must be before 'To'.";
      message.classList.add("error");
      return;
    }
    currentRange = { from, to };
    try {
      await loadDashboard();
    } catch (error) {
      message.textContent = error.message;
      message.classList.add("error");
    }
  });

  clearBtn.addEventListener("click", async () => {
    currentRange = { from: "", to: "" };
    document.getElementById("filter-from").value = "";
    document.getElementById("filter-to").value = "";
    message.textContent = "";
    message.className = "message";
    await loadDashboard();
  });
}
```

Call `setupFilterBar();` at the bottom of the file next to the existing
`setupForms().catch(...)` call.

Also update the existing `submitForm()` call to `loadDashboard()` (line ~149) — no
change needed there, it already calls the shared `loadDashboard()`, which will now
naturally respect whatever `currentRange` is set. This means logging a new brew
while a past date range is active will re-render but the new brew won't show up
until the range is cleared/widened — that's correct, expected behavior, not a bug;
no special-casing needed.

---

## Edge cases (must handle, in priority order)

1. **`from` after `to`**: reject with a 400 in the API (`"'from' must be before
   'to'"`) AND catch it client-side before even making the request (see
   `filter-apply` handler above) so the user gets instant feedback. Do not rely on
   only one of the two checks.
2. **Empty range (no brews match)**: every query must degrade gracefully to zero
   results, not error.
   - `get_stats`: `total_brews: 0`, `per_drink`: every drink type listed with
     `count: 0` (guaranteed by using the LEFT JOIN's ON-clause filter, not a WHERE
     clause — see Step 1), `per_day: []`.
   - Frontend: `renderTimeline([])` already early-returns on empty array
     (`app.js:32`, unchanged) — verify it renders an empty (blank) SVG, not an
     error. `loadDashboard()`'s `lastDay` computation
     (`stats.per_day[stats.per_day.length - 1]`) already handles empty array via
     the existing `lastDay ? lastDay.count : 0` guard — no change needed there.
   - `get_machine_health`: `brew_count: 0`, `last_brew: None`, `specialty: None`
     (already handled — `specialty["label"] if specialty else None`, unchanged),
     `last_maintenance: None`, `recent_errors: []`. Frontend already renders
     `"no brews yet"` for null specialty and `"never"` for null `last_brew`
     (`app.js:68,70`, unchanged) — verify these paths still work with a live range
     that excludes all data for a machine.
3. **Only one bound given** (open-ended range, e.g. "from 2026-06-01, no end"):
   `_range_clause` must independently handle `start`-only and `end`-only — verify
   both directions work, not just full ranges.
2b. **Days with no brews inside an otherwise non-empty range**: `per_day` only
   returns rows for days that have brews (`GROUP BY DATE(timestamp)` — unchanged
   behavior, gaps in the timeline are not zero-filled, same as today). This is
   pre-existing behavior, not a regression — do not attempt to zero-fill missing
   days, that's out of scope for this ticket.
4. **`end` bound must be exclusive of the instant, inclusive of the whole day**: a
   `datetime-local` input for "to" gives e.g. `2026-06-05T00:00` (midnight), which
   if used as `timestamp < '2026-06-05 00:00:00'` would exclude all of June 5th.
   Decide product behavior: either (a) tell users "To" means "up to this exact
   moment" (simplest, matches the raw semantics above, recommend this), or (b) if
   you want end-of-day inclusivity for a bare date, that requires additional logic
   not covered by this plan — flag it to the ticket reporter rather than guessing;
   don't silently add a day. This plan implements (a).
5. **Timezone**: all stored timestamps are naive local time (see
   `schema.py` docstring). The `datetime-local` input also produces naive local
   time with no timezone suffix. As long as you never call `.toISOString()` on the
   filter input values (that would convert to UTC and shift the range), string
   comparison stays correct. Do NOT use `localNow()`-style timezone math (used
   elsewhere in `app.js` for pre-filling the log-a-brew form) on the filter inputs —
   leave the raw `datetime-local` value untouched when sending it to the API.
6. **Malformed query param strings** (someone hand-edits the URL / calls the API
   directly with garbage): `parse_range_bound` must raise a 400 with a clear message,
   consistent with how `parse_timestamp` already behaves for POST bodies.
7. **`recent_errors`' existing `LIMIT 5`** interacting with a date range: with a
   range applied, "recent 5" now means "5 most recent within the range," which can
   surprise a user who forgets a filter is active — no code change needed, but note
   in the UI (e.g. filter bar stays visibly populated with the active dates) so
   users aren't confused why old errors "disappeared."

---

## Verification

### Automated tests

Add cases to the existing test files (they already establish good patterns to
copy):

- `tests/test_db.py`: extend `test_stats_math` and `test_machine_health` (or add
  new `test_stats_math_with_range` / `test_machine_health_with_range` functions)
  using the same fixture. Insert brews on at least 3 different days, then call
  `get_stats(conn, start="2026-06-02 00:00:00", end="2026-06-03 00:00:00")` and
  assert only the middle day's brews are counted. Also test `start=None, end=None`
  still returns everything (regression guard), and test a range with zero matches
  returns `total_brews: 0` and full-length `per_drink` list of zeros (not a
  shortened list).
- `tests/test_api.py`: extend `test_stats` and `test_machines_list_and_health` (or
  add new test functions) hitting `/api/stats?from=...&to=...` and
  `/api/machines/1?from=...&to=...` via the existing `request(app, "GET", url)`
  helper. Add a dedicated test for the `from` > `to` 400 case, e.g.:
  ```python
  def test_stats_rejects_inverted_range(db):
      r = request(app, "GET", "/api/stats?from=2026-06-05T00:00&to=2026-06-01T00:00")
      assert r.status == 400
  ```
- Run the full suite: `uv run pytest`. All existing tests must still pass
  unmodified (they call `get_stats(conn)` / hit `/api/stats` with no params — this
  must keep working identically since `start`/`end` default to `None`).

### Manual verification in the browser

1. `uv run start`, open `http://localhost:8123`.
2. Confirm the dashboard looks unchanged with no filter applied (regression check).
3. Use the "Log a brew" form to add a couple of brews with distinct, known
   timestamps a few days apart (or note existing seeded data's dates from the DB).
4. Set "From"/"To" in the new filter bar to a narrow window and click Apply —
   confirm: stat tiles change, the per-drink bars change, the timeline chart only
   shows days in range, and each machine card's specialty/brew count/last-brew
   updates to reflect only in-range data.
5. Set "From" after "To" — confirm an inline error message appears and no request
   with bad params succeeds silently.
6. Set a range with no matching brews — confirm zero counts and empty timeline
   render without a JS console error (open devtools console while doing this).
7. Click "Clear" — confirm the dashboard reverts to the full unfiltered view.
8. Reload the page — confirm the filter resets to empty/all-time (this plan does
   not add persistence across reloads; if the user wants the filter to survive a
   refresh, that's an explicit follow-up, not part of this ticket).
