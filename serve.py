#!/usr/bin/env python3
"""Local dev server: serves static files + /api/cohort proxy for PostHog embed."""

import datetime as dt
import http.server
import io
import json
import os
import re
import socketserver
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", 8765))
EMBED_TOKEN = "lH-W2rrzkDM6Ph7AIdUM4hQ-jDVsUw"
EMBED_URL = f"https://us.posthog.com/embedded/{EMBED_TOKEN}"
CACHE_TTL_SECONDS = 30

# Short-TTL response cache: collapses repeated/concurrent identical analytics
# queries (paging, re-applying a recent filter) so they don't re-hit PostHog.
RESPONSE_CACHE_TTL = 20.0  # seconds — keeps data near-live while speeding repeats
CACHEABLE_PATHS = {
    "/api/students", "/api/student", "/api/wrapped", "/api/leaders",
    "/api/trends", "/api/unileaderboard", "/api/momentum", "/api/facets",
    "/api/timeheatmap",
}
_resp_cache = {}            # full request path -> (expires_at, raw_response_bytes)
_resp_cache_lock = threading.Lock()

# HogQL query API (used by /api/students). Fill these in or set via env vars.
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.posthog.com")
POSTHOG_PROJECT_ID = os.environ.get("POSTHOG_PROJECT_ID", "238227")
POSTHOG_API_KEY = os.environ.get("POSTHOG_API_KEY", "YOUR_PERSONAL_API_KEY")

# API key placeholder — /api/students fails fast with a clear message if unset.
# (Not a startup exit: /api/cohort and static pages don't need this key.)
_KEY_PLACEHOLDER = "YOUR_PERSONAL_API_KEY"

_cache = {"at": 0.0, "payload": None}


def fetch_cohort():
    now = time.time()
    if _cache["payload"] and (now - _cache["at"] < CACHE_TTL_SECONDS):
        return _cache["payload"], None

    try:
        req = urllib.request.Request(
            EMBED_URL,
            headers={"User-Agent": "Cubbes-Scholar-Dashboard/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        m = re.search(
            r'<script id="posthog-exported-data" type="application/json">(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not m:
            return None, "embed data blob not found in PostHog response"

        data = json.loads(m.group(1))
        if isinstance(data, str):
            data = json.loads(data)

        tiles = data.get("dashboard", {}).get("tiles") or []
        if not tiles:
            return None, "no tiles in embedded dashboard"
        insight = tiles[0].get("insight") or {}

        cols = insight.get("columns") or []
        rows_raw = insight.get("result") or []

        def _norm(s):
            return "".join(ch for ch in (s or "").lower() if ch.isalnum())

        idx = {_norm(c): i for i, c in enumerate(cols)}

        def cell(row, key, default=None):
            i = idx.get(_norm(key))
            if i is None or i >= len(row):
                return default
            return row[i]

        def clean_str(v):
            if v is None:
                return None
            s = str(v).strip()
            return s or None

        rows = []
        for r in rows_raw:
            rows.append({
                "email": cell(r, "email", "") or "",
                "university": clean_str(cell(r, "University")),
                "faculty": clean_str(cell(r, "Faculty")),
                "department": clean_str(cell(r, "Department")),
                "level": clean_str(cell(r, "Level")),
                "cgpa_range": clean_str(cell(r, "CGPA_Range")),
                "gender": clean_str(cell(r, "Gender")),
                "study_minutes": cell(r, "Study_Minutes"),
                "materials_opened": cell(r, "Materials_Opened"),
                "materials_unique": cell(r, "Materials_Unique"),
                "topics_completed": cell(r, "Topics_Completed"),
                "past_questions": cell(r, "Past_Questions"),
                "ai_sessions": cell(r, "AI_Sessions"),
                "learning_streak": cell(r, "Learning_Streak"),
                "last_active": clean_str(cell(r, "Last_Active")),
            })

        payload = {
            "updated_at": insight.get("last_refresh"),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "row_count": len(rows),
            "_posthog_columns": cols,
            "rows": rows,
        }
        _cache["at"] = now
        _cache["payload"] = payload
        return payload, None

    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def posthog_query(hogql, date_range=None):
    """Run a HogQL query against the project query API; return parsed JSON."""
    query = {"kind": "HogQLQuery", "query": hogql}
    if date_range is not None:
        query["filters"] = {"dateRange": date_range}
    payload = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        f"{POSTHOG_HOST}/api/projects/{POSTHOG_PROJECT_ID}/query",
        data=payload,
        headers={
            "Authorization": f"Bearer {POSTHOG_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


# One row per student with all aggregates. `{filters}` is replaced by PostHog
# with the dateRange condition (it filters the underlying events).
STUDENTS_INNER = """
            SELECT
              p.email,
              coalesce(p.First_Name, '') AS First_Name,
              coalesce(p.Last_Name, '') AS Last_Name,
              coalesce(p.University, '') AS University,
              coalesce(p.Faculty, '') AS Faculty,
              coalesce(p.Department, '') AS Department,
              coalesce(p.Level, '') AS Level,
              round(coalesce(study.Study_Minutes, 0), 1) AS Study_Minutes,
              coalesce(m.Topics_Completed, 0) AS Topics_Completed,
              coalesce(m.Past_Questions, 0) AS Past_Questions,
              coalesce(m.AI_Sessions, 0) AS AI_Sessions,
              coalesce(m.Learning_Streak, 0) AS Learning_Streak,
              coalesce(m.Materials_Opened, 0) AS Materials_Opened,
              coalesce(m.Materials_Unique, 0) AS Materials_Unique,
              m.Last_Active,
              m.First_Active
            FROM (
              SELECT
                lower(properties.email) AS email,
                argMax(properties.First_Name, created_at) AS First_Name,
                argMax(properties.Last_Name, created_at) AS Last_Name,
                argMax(properties.University, created_at) AS University,
                argMax(properties.Faculty, created_at) AS Faculty,
                argMax(properties.Department, created_at) AS Department,
                argMax(properties.Level, created_at) AS Level
              FROM persons
              WHERE properties.email IS NOT NULL
                AND properties.email != ''
                AND coalesce(toString(properties.is_internal_user), 'false') != 'true'
              GROUP BY lower(properties.email)
            ) AS p
            LEFT JOIN (
              SELECT email, round(sum(session_seconds) / 60.0, 1) AS Study_Minutes
              FROM (
                SELECT
                  email,
                  dateDiff('second', prev_ts, timestamp) AS session_seconds
                FROM (
                  SELECT
                    lower(properties.email) AS email,
                    event,
                    timestamp,
                    lagInFrame(event, 1, '') OVER (
                      PARTITION BY lower(properties.email)
                      ORDER BY timestamp
                      ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                    ) AS prev_event,
                    lagInFrame(timestamp, 1, timestamp) OVER (
                      PARTITION BY lower(properties.email)
                      ORDER BY timestamp
                      ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                    ) AS prev_ts
                  FROM events
                  WHERE {filters}
                    AND event IN ('study_session_started', 'study_session_completed')
                    AND properties.email IS NOT NULL AND properties.email != ''
                )
                WHERE event = 'study_session_completed' AND prev_event = 'study_session_started'
              )
              WHERE session_seconds > 0 AND session_seconds < 7200
              GROUP BY email
            ) AS study ON p.email = study.email
            LEFT JOIN (
              SELECT
                lower(properties.email) AS email,
                countIf(event = 'topic_marked_complete') - countIf(event = 'topic_marked_incomplete') AS Topics_Completed,
                countIf(event = 'past_question_completed') AS Past_Questions,
                countIf(event = 'submitted_chat_prompt') AS AI_Sessions,
                maxIf(toIntOrZero(toString(properties.streak_length)), event = 'streak_day_incremented') AS Learning_Streak,
                countIf(event = 'material_opened') AS Materials_Opened,
                uniqIf(properties.material_id, event = 'material_opened') AS Materials_Unique,
                max(timestamp) AS Last_Active,
                min(timestamp) AS First_Active
              FROM events
              WHERE {filters}
                AND event IN (
                  'topic_marked_complete','topic_marked_incomplete','past_question_completed',
                  'submitted_chat_prompt','streak_day_incremented','material_opened',
                  'study_session_started','study_session_completed'
                )
                AND properties.email IS NOT NULL AND properties.email != ''
              GROUP BY lower(properties.email)
            ) AS m ON p.email = m.email
            """


def _sq(v):
    """Escape a value for a single-quoted HogQL string literal."""
    return v.replace("\\", "\\\\").replace("'", "''")


def build_filter_conds(search="", university="", level="", active="", segment=""):
    """Post-aggregation WHERE conditions for the wrapped student set (alias `s`)."""
    conds = []
    # Smart segments (recency + study history based).
    if segment == "champions":
        conds.append("s.Last_Active >= now() - toIntervalDay(7) AND s.Study_Minutes >= 60")
    elif segment == "at_risk":
        conds.append("s.Study_Minutes > 0 AND s.Last_Active < now() - toIntervalDay(7) "
                     "AND s.Last_Active >= now() - toIntervalDay(30)")
    elif segment == "hibernating":
        conds.append("s.Study_Minutes > 0 AND s.Last_Active < now() - toIntervalDay(30)")
    elif segment == "new":
        conds.append("s.First_Active >= now() - toIntervalDay(7)")
    if search:
        term = f"'{_sq(search)}'"
        conds.append(
            "(positionCaseInsensitive(s.email, {0}) > 0 "
            "OR positionCaseInsensitive(s.First_Name, {0}) > 0 "
            "OR positionCaseInsensitive(s.Last_Name, {0}) > 0 "
            "OR positionCaseInsensitive(concat(s.First_Name, ' ', s.Last_Name), {0}) > 0)"
            .format(term)
        )
    if university:
        conds.append(f"s.University = '{_sq(university)}'")
    if level:
        conds.append(f"s.Level = '{_sq(level)}'")
    if active == "today":
        conds.append("s.Last_Active >= now() - toIntervalDay(1)")
    elif active == "week":
        conds.append("s.Last_Active >= now() - toIntervalDay(7)")
    elif active == "month":
        conds.append("s.Last_Active >= now() - toIntervalDay(30)")
    elif active == "dormant":
        conds.append("(s.Last_Active < now() - toIntervalDay(30) OR s.Last_Active IS NULL)")
    return conds


class Handler(http.server.SimpleHTTPRequestHandler):
    # HTTP/1.1 keeps connections alive across the many parallel API calls.
    protocol_version = "HTTP/1.1"

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_GET(self):
        # Short-TTL cache for the expensive analytics endpoints. Captures the raw
        # response on a miss and replays it verbatim on a hit (no per-endpoint code).
        path_only = self.path.split("?", 1)[0]
        if path_only not in CACHEABLE_PATHS:
            return self._dispatch()

        now = time.time()
        with _resp_cache_lock:
            ent = _resp_cache.get(self.path)
            if ent and ent[0] > now:
                self.wfile.write(ent[1])
                return

        real_wfile = self.wfile
        buf = io.BytesIO()
        self.wfile = buf
        try:
            self._dispatch()
        finally:
            self.wfile = real_wfile

        data = buf.getvalue()
        real_wfile.write(data)
        if data.startswith(b"HTTP/1.1 200"):   # never cache error responses
            # Expire from COMPLETION time (the query can take many seconds), so a
            # follow-up request reliably hits rather than finding it already stale.
            done = time.time()
            with _resp_cache_lock:
                _resp_cache[self.path] = (done + RESPONSE_CACHE_TTL, data)
                if len(_resp_cache) > 500:      # opportunistic prune of expired keys
                    for k in [k for k, v in _resp_cache.items() if v[0] <= done]:
                        _resp_cache.pop(k, None)

    def _dispatch(self):
        if self.path.split("?", 1)[0] == "/api/cohort":
            payload, err = fetch_cohort()
            if err:
                body = json.dumps({"error": err}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/api/students"):
            if POSTHOG_API_KEY == _KEY_PLACEHOLDER:
                body = json.dumps({
                    "error": "POSTHOG_API_KEY is not set. "
                             "Run with: POSTHOG_API_KEY=phx_your_key python3 serve.py "
                             "(get a key at https://us.posthog.com/settings/user-api-keys)"
                }).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            params = parse_qs(urlparse(self.path).query)
            page = int(params.get("page", ["0"])[0])
            limit = int(params.get("limit", ["50"])[0])
            date_from = params.get("date_from", ["2026-01-01"])[0]
            date_to = params.get("date_to", [""])[0]
            search = params.get("q", [""])[0].strip()
            active = params.get("active", [""])[0].strip().lower()
            university = params.get("university", [""])[0].strip()
            level = params.get("level", [""])[0].strip()
            segment = params.get("segment", [""])[0].strip().lower()
            want_count = params.get("count", ["0"])[0] in ("1", "true", "yes")
            offset = page * limit

            # Build date range filter for PostHog
            date_range = {"date_from": date_from}
            if date_to:
                date_range["date_to"] = date_to

            inner = STUDENTS_INNER
            conds = build_filter_conds(search, university, level, active, segment)
            where = ("WHERE " + " AND ".join(conds)) if conds else ""

            data_sql = (
                f"SELECT * FROM ({inner}) AS s {where} "
                f"ORDER BY s.Study_Minutes DESC LIMIT {limit} OFFSET {offset}"
            )
            stats_sql = (
                "SELECT "
                "count() AS total, "
                "countIf(s.Study_Minutes > 0) AS active, "
                "sum(s.Study_Minutes) AS total_minutes, "
                "max(s.Learning_Streak) AS top_streak, "
                "countIf(s.Last_Active >= now() - toIntervalDay(7)) AS active_week, "
                "countIf(s.Last_Active >= now() - toIntervalDay(1)) AS active_today, "
                "countIf(s.Last_Active IS NOT NULL) AS activated, "
                "countIf(s.Study_Minutes > 0 AND s.Learning_Streak >= 3) AS habit, "
                "countIf((s.Study_Minutes > 0) + (s.Topics_Completed > 0) + (s.Past_Questions > 0) "
                "+ (s.AI_Sessions > 0) + (s.Materials_Opened > 0) >= 3) AS power_learners, "
                "countIf(s.AI_Sessions > 0) AS used_ai, "
                "countIf(s.Past_Questions > 0) AS used_pq, "
                "countIf(s.Materials_Opened > 0) AS used_materials, "
                "countIf(s.Topics_Completed > 0) AS used_topics, "
                "uniqExactIf(s.University, s.University != '') AS universities "
                f"FROM ({inner}) AS s {where}"
            )

            def run_query(hogql):
                return posthog_query(hogql, date_range)

            try:
                data = run_query(data_sql)
                out = {
                    "columns": data.get("columns"),
                    "results": data.get("results") or [],
                    "hasMore": data.get("hasMore"),
                }
                if want_count:
                    sdata = run_query(stats_sql)
                    scols = sdata.get("columns") or []
                    sres = sdata.get("results") or []
                    if sres and sres[0]:
                        row = sres[0]
                        sidx = {str(c).lower(): i for i, c in enumerate(scols)}

                        def sg(name, default=0):
                            i = sidx.get(name)
                            return row[i] if (i is not None and i < len(row)) else default

                        stats = {
                            "total": sg("total"),
                            "active": sg("active"),
                            "total_minutes": sg("total_minutes"),
                            "top_streak": sg("top_streak"),
                            "active_week": sg("active_week"),
                            "active_today": sg("active_today"),
                            "activated": sg("activated"),
                            "habit": sg("habit"),
                            "power_learners": sg("power_learners"),
                            "used_ai": sg("used_ai"),
                            "used_pq": sg("used_pq"),
                            "used_materials": sg("used_materials"),
                            "used_topics": sg("used_topics"),
                            "universities": sg("universities"),
                        }
                        out["total"] = stats["total"]
                        out["stats"] = stats
                body = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except urllib.error.HTTPError as e:
                body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return

        if self.path.split("?", 1)[0] == "/api/leaders":
            if POSTHOG_API_KEY == _KEY_PLACEHOLDER:
                body = json.dumps({"error": "POSTHOG_API_KEY is not set."}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            params = parse_qs(urlparse(self.path).query)
            date_from = params.get("date_from", ["2026-01-01"])[0]
            date_to = params.get("date_to", [""])[0]
            search = params.get("q", [""])[0].strip()
            active = params.get("active", [""])[0].strip().lower()
            university = params.get("university", [""])[0].strip()
            level = params.get("level", [""])[0].strip()
            date_range = {"date_from": date_from}
            if date_to:
                date_range["date_to"] = date_to

            # Top group by avg study time among active students. Each dimension is
            # only computed when it isn't already pinned by a filter.
            parts = []
            if not university:
                uc = build_filter_conds(search, "", level, active) + \
                    ["s.University != ''", "s.Study_Minutes > 0"]
                parts.append(
                    "(SELECT 'university' AS kind, s.University AS label, "
                    "round(avg(s.Study_Minutes), 1) AS avg_min, count() AS n "
                    f"FROM ({STUDENTS_INNER}) AS s WHERE {' AND '.join(uc)} "
                    "GROUP BY s.University HAVING count() >= 3 ORDER BY avg_min DESC LIMIT 1)"
                )
            if not level:
                lc = build_filter_conds(search, university, "", active) + \
                    ["s.Level != ''", "s.Study_Minutes > 0"]
                parts.append(
                    "(SELECT 'level' AS kind, s.Level AS label, "
                    "round(avg(s.Study_Minutes), 1) AS avg_min, count() AS n "
                    f"FROM ({STUDENTS_INNER}) AS s WHERE {' AND '.join(lc)} "
                    "GROUP BY s.Level HAVING count() >= 3 ORDER BY avg_min DESC LIMIT 1)"
                )

            out = {"university": None, "level": None}
            try:
                if parts:
                    data = posthog_query(" UNION ALL ".join(parts), date_range)
                    for row in (data.get("results") or []):
                        if not row:
                            continue
                        kind = row[0]
                        rec = {"label": row[1], "avg_min": row[2], "n": row[3]}
                        if kind in out:
                            out[kind] = rec
                body = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except urllib.error.HTTPError as e:
                body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return

        if self.path.split("?", 1)[0] == "/api/wrapped":
            if POSTHOG_API_KEY == _KEY_PLACEHOLDER:
                body = json.dumps({"error": "POSTHOG_API_KEY is not set."}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            params = parse_qs(urlparse(self.path).query)
            email = params.get("email", [""])[0].strip()
            date_from = params.get("date_from", ["2026-01-01"])[0]
            date_to = params.get("date_to", [""])[0]
            if not email:
                body = json.dumps({"error": "email is required"}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            date_range = {"date_from": date_from}
            if date_to:
                date_range["date_to"] = date_to
            em = _sq(email)

            out = {"email": email, "period": {"date_from": date_from, "date_to": date_to}}

            # Profile (persons table; no date filter)
            try:
                profile_sql = (
                    "SELECT "
                    "argMax(properties.First_Name, created_at) AS First_Name, "
                    "argMax(properties.Last_Name, created_at) AS Last_Name, "
                    "argMax(properties.University, created_at) AS University, "
                    "argMax(properties.Faculty, created_at) AS Faculty, "
                    "argMax(properties.Department, created_at) AS Department, "
                    "argMax(properties.Level, created_at) AS Level "
                    f"FROM persons WHERE lower(properties.email) = lower('{em}') "
                    "GROUP BY lower(properties.email)"
                )
                pd = posthog_query(profile_sql)
                pr = (pd.get("results") or [[None] * 6])
                row = pr[0] if pr else [None] * 6

                def pv(i):
                    return (str(row[i]).strip() if (i < len(row) and row[i] not in (None, "")) else None)

                first = pv(0) or ""
                last = pv(1) or ""
                out["profile"] = {
                    "name": (first + " " + last).strip(),
                    "university": pv(2), "faculty": pv(3),
                    "department": pv(4), "level": pv(5),
                }
            except Exception as exc:
                out["profile_error"] = f"{type(exc).__name__}: {exc}"
                out["profile"] = {"name": ""}

            # Feature counts + active-day stats
            try:
                feat_sql = (
                    "SELECT "
                    "countIf(event = 'topic_marked_complete') - countIf(event = 'topic_marked_incomplete') AS topics, "
                    "countIf(event = 'past_question_completed') AS past_questions, "
                    "countIf(event = 'submitted_chat_prompt') AS ai_sessions, "
                    "maxIf(toIntOrZero(toString(properties.streak_length)), event = 'streak_day_incremented') AS streak, "
                    "countIf(event = 'material_opened') AS materials_opened, "
                    "uniqIf(properties.material_id, event = 'material_opened') AS materials_unique, "
                    "uniqExact(toStartOfDay(timestamp)) AS active_days, "
                    "min(timestamp) AS first_active, max(timestamp) AS last_active "
                    f"FROM events WHERE {{filters}} AND lower(properties.email) = lower('{em}') "
                    "AND event IN ('topic_marked_complete','topic_marked_incomplete','past_question_completed',"
                    "'submitted_chat_prompt','streak_day_incremented','material_opened',"
                    "'study_session_started','study_session_completed')"
                )
                fd = posthog_query(feat_sql, date_range)
                fr = (fd.get("results") or [[0, 0, 0, 0, 0, 0, 0, None, None]])[0]
                fcols = fd.get("columns") or []
                fidx = {str(c).lower(): i for i, c in enumerate(fcols)}

                def fg(name, default=0):
                    i = fidx.get(name)
                    return fr[i] if (i is not None and i < len(fr)) else default

                out.update({
                    "topics": fg("topics"), "past_questions": fg("past_questions"),
                    "ai_sessions": fg("ai_sessions"), "streak": fg("streak") or 0,
                    "materials_opened": fg("materials_opened"),
                    "materials_unique": fg("materials_unique"),
                    "active_days": fg("active_days"),
                    "first_active": fg("first_active", None),
                    "last_active": fg("last_active", None),
                })
            except Exception as exc:
                out["features_error"] = f"{type(exc).__name__}: {exc}"

            # Study minutes + completed sessions (session pairing)
            study_minutes = 0
            try:
                study_sql = (
                    "SELECT round(sum(session_seconds) / 60.0, 1) AS study_minutes, count() AS sessions FROM ("
                    "SELECT dateDiff('second', prev_ts, timestamp) AS session_seconds, event, prev_event FROM ("
                    "SELECT event, timestamp, "
                    "lagInFrame(event, 1, '') OVER (PARTITION BY lower(properties.email) ORDER BY timestamp "
                    "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS prev_event, "
                    "lagInFrame(timestamp, 1, timestamp) OVER (PARTITION BY lower(properties.email) ORDER BY timestamp "
                    "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS prev_ts "
                    f"FROM events WHERE {{filters}} AND lower(properties.email) = lower('{em}') "
                    "AND event IN ('study_session_started','study_session_completed')"
                    ") WHERE event = 'study_session_completed' AND prev_event = 'study_session_started') "
                    "WHERE session_seconds > 0 AND session_seconds < 7200"
                )
                sd = posthog_query(study_sql, date_range)
                sr = (sd.get("results") or [[0, 0]])[0]
                study_minutes = sr[0] or 0
                out["study_minutes"] = study_minutes
                out["sessions"] = sr[1] if len(sr) > 1 else 0
            except Exception as exc:
                out["study_error"] = f"{type(exc).__name__}: {exc}"
                out["study_minutes"] = 0
                out["sessions"] = 0

            # Rank / percentile among active studiers (heavy: runs the full inner)
            if study_minutes and study_minutes > 0:
                try:
                    rank_sql = (
                        f"SELECT countIf(s.Study_Minutes > {study_minutes}) AS ahead, "
                        "count() AS total_active "
                        f"FROM ({STUDENTS_INNER}) AS s WHERE s.Study_Minutes > 0"
                    )
                    rd = posthog_query(rank_sql, date_range)
                    rr = (rd.get("results") or [[0, 0]])[0]
                    ahead = rr[0] or 0
                    total_active = rr[1] or 0
                    pct = round(((ahead + 1) / total_active) * 100) if total_active else None
                    out["rank"] = {
                        "ahead": ahead, "total_active": total_active,
                        "position": ahead + 1,
                        "percentile_top": (max(1, pct) if pct is not None else None),
                    }
                except Exception as exc:
                    out["rank_error"] = f"{type(exc).__name__}: {exc}"

            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.split("?", 1)[0] == "/api/student":
            if POSTHOG_API_KEY == _KEY_PLACEHOLDER:
                body = json.dumps({"error": "POSTHOG_API_KEY is not set."}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            params = parse_qs(urlparse(self.path).query)
            email = params.get("email", [""])[0].strip()
            date_from = params.get("date_from", ["2026-01-01"])[0]
            date_to = params.get("date_to", [""])[0]
            try:
                limit = min(200, max(1, int(params.get("limit", ["60"])[0])))
            except ValueError:
                limit = 60

            if not email:
                body = json.dumps({"error": "email is required"}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            date_range = {"date_from": date_from}
            if date_to:
                date_range["date_to"] = date_to

            timeline_sql = (
                "SELECT timestamp, event, "
                "properties.material_id AS material_id, "
                "properties.streak_length AS streak_length "
                "FROM events "
                "WHERE {filters} "
                f"AND lower(properties.email) = lower('{_sq(email)}') "
                "AND event IN ("
                "'study_session_started','study_session_completed','topic_marked_complete',"
                "'topic_marked_incomplete','past_question_completed','submitted_chat_prompt',"
                "'streak_day_incremented','material_opened') "
                f"ORDER BY timestamp DESC LIMIT {limit}"
            )

            try:
                data = posthog_query(timeline_sql, date_range)
                cols = data.get("columns") or []
                cidx = {str(c).lower(): i for i, c in enumerate(cols)}

                def cg(row, name):
                    i = cidx.get(name)
                    return row[i] if (i is not None and i < len(row)) else None

                events = [
                    {
                        "ts": cg(r, "timestamp"),
                        "event": cg(r, "event"),
                        "material_id": cg(r, "material_id"),
                        "streak_length": cg(r, "streak_length"),
                    }
                    for r in (data.get("results") or []) if r
                ]
                out = {"email": email, "events": events, "daily": []}

                # Daily activity counts (for the contribution heatmap).
                try:
                    daily_sql = (
                        "SELECT toStartOfDay(timestamp) AS day, count() AS events "
                        "FROM events WHERE {filters} "
                        f"AND lower(properties.email) = lower('{_sq(email)}') "
                        "AND event IN ("
                        "'study_session_started','study_session_completed','topic_marked_complete',"
                        "'topic_marked_incomplete','past_question_completed','submitted_chat_prompt',"
                        "'streak_day_incremented','material_opened') "
                        "GROUP BY day ORDER BY day"
                    )
                    dd = posthog_query(daily_sql, date_range)
                    out["daily"] = [
                        {"day": r[0], "count": r[1]}
                        for r in (dd.get("results") or []) if r
                    ]
                except Exception as exc:
                    out["daily_error"] = f"{type(exc).__name__}: {exc}"

                body = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except urllib.error.HTTPError as e:
                body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return

        if self.path.split("?", 1)[0] == "/api/trends":
            if POSTHOG_API_KEY == _KEY_PLACEHOLDER:
                body = json.dumps({"error": "POSTHOG_API_KEY is not set."}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            params = parse_qs(urlparse(self.path).query)
            date_from = params.get("date_from", ["2026-01-01"])[0]
            date_to = params.get("date_to", [""])[0]
            search = params.get("q", [""])[0].strip()
            university = params.get("university", [""])[0].strip()
            level = params.get("level", [""])[0].strip()
            date_range = {"date_from": date_from}
            if date_to:
                date_range["date_to"] = date_to

            STUDY_EVENTS = "('study_session_started', 'study_session_completed')"

            # Restrict events to the persons matching the active filters (uni/level/search).
            pconds = []
            if university:
                pconds.append(f"pp.University = '{_sq(university)}'")
            if level:
                pconds.append(f"pp.Level = '{_sq(level)}'")
            if search:
                term = f"'{_sq(search)}'"
                pconds.append(
                    "(positionCaseInsensitive(pp.email, {0}) > 0 "
                    "OR positionCaseInsensitive(pp.First_Name, {0}) > 0 "
                    "OR positionCaseInsensitive(pp.Last_Name, {0}) > 0 "
                    "OR positionCaseInsensitive(concat(pp.First_Name, ' ', pp.Last_Name), {0}) > 0)"
                    .format(term)
                )
            persons_agg = (
                "SELECT lower(properties.email) AS email, "
                "argMax(properties.University, created_at) AS University, "
                "argMax(properties.Level, created_at) AS Level, "
                "argMax(properties.First_Name, created_at) AS First_Name, "
                "argMax(properties.Last_Name, created_at) AS Last_Name "
                "FROM persons WHERE properties.email IS NOT NULL AND properties.email != '' "
                "AND coalesce(toString(properties.is_internal_user), 'false') != 'true' "
                "GROUP BY lower(properties.email)"
            )
            email_in = ""
            if pconds:
                email_in = (
                    f" AND lower(properties.email) IN "
                    f"(SELECT pp.email FROM ({persons_agg}) AS pp WHERE {' AND '.join(pconds)})"
                )

            out = {"series": [], "weekly": None, "breakdown": []}

            # 1) Daily activity over the selected range.
            try:
                series_sql = (
                    "SELECT toStartOfDay(timestamp) AS day, "
                    "uniqExact(lower(properties.email)) AS active, "
                    "countIf(event = 'study_session_completed') AS sessions "
                    f"FROM events WHERE {{filters}} AND event IN {STUDY_EVENTS} "
                    "AND properties.email IS NOT NULL AND properties.email != ''"
                    f"{email_in} GROUP BY day ORDER BY day"
                )
                sd = posthog_query(series_sql, date_range)
                out["series"] = [
                    {"day": r[0], "active": r[1], "sessions": r[2]}
                    for r in (sd.get("results") or []) if r
                ]
            except Exception as exc:
                out["series_error"] = f"{type(exc).__name__}: {exc}"

            # 2) Week-over-week: last 7 days vs the prior 7 days.
            try:
                weekly_sql = (
                    "SELECT "
                    "uniqExactIf(lower(properties.email), event IN " + STUDY_EVENTS +
                    " AND timestamp >= now() - toIntervalDay(7)) AS cur_active, "
                    "uniqExactIf(lower(properties.email), event IN " + STUDY_EVENTS +
                    " AND timestamp < now() - toIntervalDay(7)) AS prev_active, "
                    "countIf(event = 'study_session_completed' AND timestamp >= now() - toIntervalDay(7)) AS cur_sessions, "
                    "countIf(event = 'study_session_completed' AND timestamp < now() - toIntervalDay(7)) AS prev_sessions, "
                    "countIf(event = 'submitted_chat_prompt' AND timestamp >= now() - toIntervalDay(7)) AS cur_ai, "
                    "countIf(event = 'submitted_chat_prompt' AND timestamp < now() - toIntervalDay(7)) AS prev_ai, "
                    "countIf(event = 'past_question_completed' AND timestamp >= now() - toIntervalDay(7)) AS cur_pq, "
                    "countIf(event = 'past_question_completed' AND timestamp < now() - toIntervalDay(7)) AS prev_pq "
                    "FROM events WHERE {filters} "
                    "AND properties.email IS NOT NULL AND properties.email != ''"
                    f"{email_in}"
                )
                wd = posthog_query(weekly_sql, {"date_from": "-14d"})
                wres = (wd.get("results") or [[0] * 8])[0]
                wcols = wd.get("columns") or []
                widx = {str(c).lower(): i for i, c in enumerate(wcols)}

                def wg(name):
                    i = widx.get(name)
                    return wres[i] if (i is not None and i < len(wres)) else 0

                out["weekly"] = {
                    "active": {"current": wg("cur_active"), "previous": wg("prev_active")},
                    "sessions": {"current": wg("cur_sessions"), "previous": wg("prev_sessions")},
                    "ai": {"current": wg("cur_ai"), "previous": wg("prev_ai")},
                    "past_questions": {"current": wg("cur_pq"), "previous": wg("prev_pq")},
                }
            except Exception as exc:
                out["weekly_error"] = f"{type(exc).__name__}: {exc}"

            # 3) Top universities by active students in range (skip if pinned to one uni).
            if not university:
                try:
                    bp = list(pconds)  # level/search still apply; university is the group key
                    bp.append("pp.University != ''")
                    breakdown_sql = (
                        "SELECT pp.University AS label, uniqExact(pp.email) AS active "
                        f"FROM ({persons_agg}) AS pp "
                        "WHERE pp.email IN (SELECT DISTINCT lower(properties.email) FROM events "
                        f"WHERE {{filters}} AND event IN {STUDY_EVENTS} "
                        "AND properties.email IS NOT NULL AND properties.email != '') "
                        f"AND {' AND '.join(bp)} "
                        "GROUP BY pp.University ORDER BY active DESC LIMIT 8"
                    )
                    bd = posthog_query(breakdown_sql, date_range)
                    out["breakdown"] = [
                        {"label": r[0], "active": r[1]}
                        for r in (bd.get("results") or []) if r and r[0]
                    ]
                except Exception as exc:
                    out["breakdown_error"] = f"{type(exc).__name__}: {exc}"

            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.split("?", 1)[0] == "/api/unileaderboard":
            if POSTHOG_API_KEY == _KEY_PLACEHOLDER:
                body = json.dumps({"error": "POSTHOG_API_KEY is not set."}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            params = parse_qs(urlparse(self.path).query)
            date_from = params.get("date_from", ["2026-01-01"])[0]
            date_to = params.get("date_to", [""])[0]
            search = params.get("q", [""])[0].strip()
            level = params.get("level", [""])[0].strip()

            try:
                df_d = dt.date.fromisoformat(date_from)
            except ValueError:
                df_d = dt.date(2026, 1, 1)
            de_d = None
            if date_to:
                try:
                    de_d = dt.date.fromisoformat(date_to)
                except ValueError:
                    de_d = None
            if de_d is None:
                de_d = dt.datetime.now(dt.timezone.utc).date()
            length = max(1, (de_d - df_d).days)
            prev_start = df_d - dt.timedelta(days=length)
            cut = df_d.isoformat()
            window = {"date_from": prev_start.isoformat(), "date_to": de_d.isoformat()}

            pconds = ["pp.University != ''"]
            if level:
                pconds.append(f"pp.Level = '{_sq(level)}'")
            if search:
                term = f"'{_sq(search)}'"
                pconds.append(
                    "(positionCaseInsensitive(pp.email, {0}) > 0 "
                    "OR positionCaseInsensitive(pp.First_Name, {0}) > 0 "
                    "OR positionCaseInsensitive(pp.Last_Name, {0}) > 0)".format(term)
                )
            persons_agg = (
                "SELECT lower(properties.email) AS email, "
                "argMax(properties.University, created_at) AS University, "
                "argMax(properties.Level, created_at) AS Level, "
                "argMax(properties.First_Name, created_at) AS First_Name, "
                "argMax(properties.Last_Name, created_at) AS Last_Name "
                "FROM persons WHERE properties.email IS NOT NULL AND properties.email != '' "
                "AND coalesce(toString(properties.is_internal_user), 'false') != 'true' "
                "GROUP BY lower(properties.email)"
            )
            lb_sql = (
                "SELECT pp.University AS label, "
                f"uniqExactIf(ev.email, ev.ts >= toDateTime('{cut} 00:00:00')) AS cur, "
                f"uniqExactIf(ev.email, ev.ts < toDateTime('{cut} 00:00:00')) AS prev "
                "FROM (SELECT lower(properties.email) AS email, timestamp AS ts FROM events "
                "WHERE {filters} AND event IN ('study_session_started','study_session_completed') "
                "AND properties.email IS NOT NULL AND properties.email != '') AS ev "
                f"INNER JOIN ({persons_agg}) AS pp ON ev.email = pp.email "
                f"WHERE {' AND '.join(pconds)} "
                "GROUP BY pp.University HAVING cur > 0 OR prev > 0"
            )

            out = {"leaders": []}
            try:
                data = posthog_query(lb_sql, window)
                rows = [
                    {"label": r[0], "cur": r[1] or 0, "prev": r[2] or 0}
                    for r in (data.get("results") or []) if r and r[0]
                ]
                cur_order = sorted(rows, key=lambda x: (-x["cur"], x["label"]))
                prev_order = sorted([r for r in rows if r["prev"] > 0], key=lambda x: (-x["prev"], x["label"]))
                prev_rank = {r["label"]: i + 1 for i, r in enumerate(prev_order)}
                leaders = []
                for i, r in enumerate(cur_order[:10]):
                    pr = prev_rank.get(r["label"])
                    leaders.append({
                        "label": r["label"], "active": r["cur"], "prev_active": r["prev"],
                        "rank": i + 1, "prev_rank": pr,
                        "movement": (pr - (i + 1)) if pr else None,
                    })
                out["leaders"] = leaders
            except Exception as exc:
                out["error"] = f"{type(exc).__name__}: {exc}"

            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.split("?", 1)[0] == "/api/timeheatmap":
            if POSTHOG_API_KEY == _KEY_PLACEHOLDER:
                body = json.dumps({"error": "POSTHOG_API_KEY is not set."}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            params = parse_qs(urlparse(self.path).query)
            date_from = params.get("date_from", ["2026-01-01"])[0]
            date_to = params.get("date_to", [""])[0]
            search = params.get("q", [""])[0].strip()
            university = params.get("university", [""])[0].strip()
            level = params.get("level", [""])[0].strip()
            date_range = {"date_from": date_from}
            if date_to:
                date_range["date_to"] = date_to

            pconds = []
            if university:
                pconds.append(f"pp.University = '{_sq(university)}'")
            if level:
                pconds.append(f"pp.Level = '{_sq(level)}'")
            if search:
                term = f"'{_sq(search)}'"
                pconds.append(
                    "(positionCaseInsensitive(pp.email, {0}) > 0 "
                    "OR positionCaseInsensitive(pp.First_Name, {0}) > 0 "
                    "OR positionCaseInsensitive(pp.Last_Name, {0}) > 0)".format(term)
                )
            email_in = ""
            if pconds:
                persons_agg = (
                    "SELECT lower(properties.email) AS email, "
                    "argMax(properties.University, created_at) AS University, "
                    "argMax(properties.Level, created_at) AS Level, "
                    "argMax(properties.First_Name, created_at) AS First_Name, "
                    "argMax(properties.Last_Name, created_at) AS Last_Name "
                    "FROM persons WHERE properties.email IS NOT NULL AND properties.email != '' "
                    "AND coalesce(toString(properties.is_internal_user), 'false') != 'true' "
                    "GROUP BY lower(properties.email)"
                )
                email_in = (f" AND lower(properties.email) IN "
                            f"(SELECT pp.email FROM ({persons_agg}) AS pp WHERE {' AND '.join(pconds)})")

            # Day-of-week (1=Mon..7=Sun) × hour-of-day (0..23), shifted UTC->WAT (+1h).
            out = {"cells": [], "tz": "WAT (UTC+1)"}
            try:
                hm_sql = (
                    "SELECT toDayOfWeek(timestamp + toIntervalHour(1)) AS dow, "
                    "toHour(timestamp + toIntervalHour(1)) AS hour, count() AS c "
                    "FROM events WHERE {filters} "
                    "AND event IN ('study_session_started','study_session_completed') "
                    "AND properties.email IS NOT NULL AND properties.email != ''"
                    f"{email_in} "
                    "GROUP BY dow, hour"
                )
                hd = posthog_query(hm_sql, date_range)
                out["cells"] = [
                    {"dow": r[0], "hour": r[1], "c": r[2] or 0}
                    for r in (hd.get("results") or []) if r
                ]
            except Exception as exc:
                out["error"] = f"{type(exc).__name__}: {exc}"

            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.split("?", 1)[0] == "/api/momentum":
            if POSTHOG_API_KEY == _KEY_PLACEHOLDER:
                body = json.dumps({"error": "POSTHOG_API_KEY is not set."}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            params = parse_qs(urlparse(self.path).query)
            emails = [e.strip().lower() for e in params.get("emails", [""])[0].split(",") if e.strip()]
            # de-dupe, preserve order, cap
            seen = set()
            emails = [e for e in emails if not (e in seen or seen.add(e))][:120]

            out = {"momentum": {}}
            if emails:
                in_list = ",".join("'" + _sq(e) + "'" for e in emails)
                mom_sql = (
                    "SELECT lower(properties.email) AS email, toStartOfDay(timestamp) AS day, count() AS c "
                    "FROM events WHERE {filters} "
                    f"AND lower(properties.email) IN ({in_list}) "
                    "AND event IN ('study_session_started','study_session_completed','topic_marked_complete',"
                    "'past_question_completed','submitted_chat_prompt','material_opened') "
                    "GROUP BY email, day"
                )
                try:
                    data = posthog_query(mom_sql, {"date_from": "-14d"})
                    today = dt.datetime.now(dt.timezone.utc).date()
                    days7 = [(today - dt.timedelta(days=6 - i)).isoformat() for i in range(7)]
                    prev7 = [(today - dt.timedelta(days=13 - i)).isoformat() for i in range(7)]
                    by = {}
                    for r in (data.get("results") or []):
                        if not r:
                            continue
                        em = r[0]
                        day = str(r[1])[:10]
                        by.setdefault(em, {})[day] = r[2] or 0
                    mom = {}
                    for em in emails:
                        dd = by.get(em, {})
                        spark = [dd.get(k, 0) for k in days7]
                        last7 = sum(spark)
                        pv = sum(dd.get(k, 0) for k in prev7)
                        mom[em] = {
                            "spark": spark, "last7": last7, "prev7": pv,
                            "dir": "up" if last7 > pv else ("down" if last7 < pv else "flat"),
                        }
                    out["momentum"] = mom
                except Exception as exc:
                    out["error"] = f"{type(exc).__name__}: {exc}"

            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.split("?", 1)[0] == "/api/facets":
            if POSTHOG_API_KEY == _KEY_PLACEHOLDER:
                body = json.dumps({"error": "POSTHOG_API_KEY is not set."}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            facet_sql = """
            SELECT
              arraySort(groupUniqArray(University)) AS universities,
              arraySort(groupUniqArray(Level)) AS levels
            FROM (
              SELECT
                lower(properties.email) AS email,
                coalesce(argMax(properties.University, created_at), '') AS University,
                coalesce(argMax(properties.Level, created_at), '') AS Level
              FROM persons
              WHERE properties.email IS NOT NULL
                AND properties.email != ''
                AND coalesce(toString(properties.is_internal_user), 'false') != 'true'
              GROUP BY lower(properties.email)
            )
            """
            try:
                data = posthog_query(facet_sql)
                res = data.get("results") or []
                row = res[0] if res else [[], []]
                universities = sorted({str(u).strip() for u in (row[0] or []) if u and str(u).strip()})
                levels = sorted(
                    {str(l).strip() for l in (row[1] or []) if l and str(l).strip()},
                    key=lambda v: (0, int(v)) if v.isdigit() else (1, 0),
                )
                body = json.dumps({"universities": universities, "levels": levels}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except urllib.error.HTTPError as e:
                body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return

        super().do_GET()

    def log_message(self, fmt, *args):
        return  # quiet


class ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with ThreadingServer(("", PORT), Handler) as httpd:
        print(f"Serving on http://localhost:{PORT}")
        httpd.serve_forever()
