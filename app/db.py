"""PostgreSQL persistence layer (psycopg 3).

If the database is temporarily unreachable the application still starts:
schema creation and queries are guarded, and the UI reports storage status —
this keeps the workbench usable in restricted CI environments while the
documented deployment (docker compose) always has Postgres available.
"""
from __future__ import annotations

from typing import Any, Optional

import psycopg
from psycopg.types.json import Json

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id              TEXT PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    baseline_name   TEXT NOT NULL,
    candidate_name  TEXT NOT NULL,
    baseline_meta   JSONB NOT NULL,
    candidate_meta  JSONB NOT NULL,
    report          JSONB NOT NULL,
    stats           JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS analyses_created_idx ON analyses (created_at DESC);

CREATE TABLE IF NOT EXISTS analysis_packages (
    id          TEXT PRIMARY KEY
                REFERENCES analyses(id) ON DELETE CASCADE,
    baseline    JSONB NOT NULL,
    candidate   JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_coverage (
    analysis_id TEXT NOT NULL
                REFERENCES analyses(id) ON DELETE CASCADE,
    side        TEXT NOT NULL,
    doc         JSONB NOT NULL,
    filename    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (analysis_id, side)
);
"""


class Database:
    def __init__(self, url: Optional[str] = None):
        self.url = url or settings.database_url
        self.available = False
        self.last_error: Optional[str] = None

    def connect(self) -> "Database":
        try:
            with psycopg.connect(self.url, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(_SCHEMA)
                conn.commit()
            self.available = True
            self.last_error = None
        except Exception as exc:  # pragma: no cover - environment dependent
            self.available = False
            self.last_error = str(exc)
        return self

    def _conn(self):
        return psycopg.connect(self.url, connect_timeout=5)

    def save_analysis(self, analysis_id: str, baseline_name: str,
                      candidate_name: str, baseline_meta: dict,
                      candidate_meta: dict, report: dict,
                      stats: dict) -> bool:
        if not self.available:
            return False
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO analyses (id, baseline_name, candidate_name,
                                             baseline_meta, candidate_meta,
                                             report, stats)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            report = EXCLUDED.report,
                            stats = EXCLUDED.stats,
                            created_at = now()
                        """,
                        (analysis_id, baseline_name, candidate_name,
                         Json(baseline_meta), Json(candidate_meta),
                         Json(report), Json(stats)))
                conn.commit()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def list_analyses(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.available:
            return []
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, created_at, baseline_name, candidate_name,
                               baseline_meta, candidate_meta, stats
                        FROM analyses
                        ORDER BY created_at DESC, id
                        LIMIT %s
                        """, (limit,))
                    rows = cur.fetchall()
            out = []
            for r in rows:
                out.append({
                    "id": r[0],
                    "created_at": r[1].isoformat() if r[1] else None,
                    "baseline_name": r[2],
                    "candidate_name": r[3],
                    "baseline_meta": r[4],
                    "candidate_meta": r[5],
                    "stats": r[6],
                })
            return out
        except Exception as exc:
            self.last_error = str(exc)
            return []

    def get_analysis(self, analysis_id: str) -> Optional[dict[str, Any]]:
        if not self.available:
            return None
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, created_at, baseline_name, candidate_name,"
                        " baseline_meta, candidate_meta, report, stats "
                        "FROM analyses WHERE id = %s", (analysis_id,))
                    r = cur.fetchone()
            if not r:
                return None
            return {
                "id": r[0],
                "created_at": r[1].isoformat() if r[1] else None,
                "baseline_name": r[2],
                "candidate_name": r[3],
                "baseline_meta": r[4],
                "candidate_meta": r[5],
                "report": r[6],
                "stats": r[7],
            }
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def delete_analysis(self, analysis_id: str) -> bool:
        if not self.available:
            return False
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM analyses WHERE id = %s",
                                (analysis_id,))
                conn.commit()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def save_packages(self, analysis_id: str, baseline: dict,
                      candidate: dict) -> bool:
        if not self.available:
            return False
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO analysis_packages (id, baseline, candidate)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            baseline = EXCLUDED.baseline,
                            candidate = EXCLUDED.candidate
                        """,
                        (analysis_id, Json(baseline), Json(candidate)))
                conn.commit()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def get_packages(self, analysis_id: str) -> Optional[dict[str, Any]]:
        if not self.available:
            return None
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT baseline, candidate FROM analysis_packages "
                        "WHERE id = %s", (analysis_id,))
                    r = cur.fetchone()
            if not r:
                return None
            return {"baseline": r[0], "candidate": r[1]}
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def status(self) -> dict:
        return {"available": self.available, "error": self.last_error}

    # ---------------------------------------------------------- coverage
    def save_coverage(self, analysis_id: str, side: str, doc: dict,
                      filename: Optional[str], timestamp: str) -> bool:
        if not self.available:
            return False
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO analysis_coverage
                            (analysis_id, side, doc, filename, created_at,
                             updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (analysis_id, side) DO UPDATE SET
                            doc = EXCLUDED.doc,
                            filename = EXCLUDED.filename,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (analysis_id, side, Json(doc), filename, timestamp,
                         timestamp))
                conn.commit()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def get_coverage(self, analysis_id: str,
                     side: str) -> Optional[dict[str, Any]]:
        if not self.available:
            return None
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT doc, filename, created_at, updated_at "
                        "FROM analysis_coverage "
                        "WHERE analysis_id = %s AND side = %s",
                        (analysis_id, side))
                    r = cur.fetchone()
            if not r:
                return None
            return {"doc": r[0], "filename": r[1],
                    "created_at": r[2].isoformat() if r[2] else None,
                    "updated_at": r[3].isoformat() if r[3] else None}
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def list_coverage(self, analysis_id: str) -> list[dict[str, Any]]:
        if not self.available:
            return []
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT side, filename, created_at, updated_at "
                        "FROM analysis_coverage WHERE analysis_id = %s "
                        "ORDER BY side", (analysis_id,))
                    rows = cur.fetchall()
            return [{"side": r[0], "filename": r[1],
                     "created_at": r[2].isoformat() if r[2] else None,
                     "updated_at": r[3].isoformat() if r[3] else None}
                    for r in rows]
        except Exception as exc:
            self.last_error = str(exc)
            return []

    def delete_coverage(self, analysis_id: str, side: str) -> bool:
        if not self.available:
            return False
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM analysis_coverage "
                        "WHERE analysis_id = %s AND side = %s",
                        (analysis_id, side))
                conn.commit()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False


db = Database()
