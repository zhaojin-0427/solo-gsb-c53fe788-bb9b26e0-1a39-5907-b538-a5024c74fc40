"""Storage facade: PostgreSQL when available, in-memory fallback otherwise.

The fallback keeps the workbench fully functional in environments without a
database (local try-outs, CI); the docker-compose deployment always uses
PostgreSQL and reports it via ``/api/health``.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from .db import db


class _MemoryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._analyses: dict[str, dict] = {}
        self._packages: dict[str, dict] = {}
        self._coverage: dict[tuple[str, str], dict] = {}

    @property
    def available(self) -> bool:
        return False  # signals degraded mode

    def status(self) -> dict:
        return {"available": False, "mode": "memory",
                "error": "PostgreSQL 不可用，使用进程内存储（重启丢失）"}

    def save(self, analysis: dict, packages: dict) -> bool:
        with self._lock:
            self._analyses[analysis["id"]] = analysis
            self._packages[analysis["id"]] = packages
        return True

    def list_analyses(self, limit: int = 50):
        with self._lock:
            items = sorted(self._analyses.values(),
                           key=lambda a: a.get("created_at", ""),
                           reverse=True)
        return [{"id": a["id"],
                 "created_at": a.get("created_at"),
                 "baseline_name": a["baseline_name"],
                 "candidate_name": a["candidate_name"],
                 "baseline_meta": a["baseline_meta"],
                 "candidate_meta": a["candidate_meta"],
                 "stats": a["stats"]} for a in items[:limit]]

    def get(self, analysis_id: str) -> Optional[dict]:
        with self._lock:
            return self._analyses.get(analysis_id)

    def get_packages(self, analysis_id: str) -> Optional[dict]:
        with self._lock:
            return self._packages.get(analysis_id)

    def delete(self, analysis_id: str) -> bool:
        with self._lock:
            existed = analysis_id in self._analyses
            self._analyses.pop(analysis_id, None)
            self._packages.pop(analysis_id, None)
            for key in [k for k in self._coverage if k[0] == analysis_id]:
                self._coverage.pop(key, None)
        return existed

    def save_coverage(self, analysis_id: str, side: str, doc: dict,
                      filename: str | None, timestamp: str) -> bool:
        with self._lock:
            existed = (analysis_id, side) in self._coverage
            created_at = self._coverage[(analysis_id, side)]["created_at"] \
                if existed else timestamp
            self._coverage[(analysis_id, side)] = {
                "doc": doc, "filename": filename,
                "created_at": created_at, "updated_at": timestamp}
        return True

    def get_coverage(self, analysis_id: str, side: str):
        with self._lock:
            rec = self._coverage.get((analysis_id, side))
            return dict(rec) if rec else None

    def list_coverage(self, analysis_id: str):
        with self._lock:
            return [{"side": side, "filename": rec["filename"],
                     "created_at": rec["created_at"],
                     "updated_at": rec["updated_at"]}
                    for (aid, side), rec in sorted(self._coverage.items())
                    if aid == analysis_id]

    def delete_coverage(self, analysis_id: str, side: str) -> bool:
        with self._lock:
            existed = (analysis_id, side) in self._coverage
            self._coverage.pop((analysis_id, side), None)
        return existed


_memory = _MemoryStore()


class Store:
    def __init__(self):
        self.db = db

    @property
    def is_postgres(self) -> bool:
        return self.db.available

    def status(self) -> dict:
        if self.db.available:
            s = self.db.status()
            return {"available": True, "mode": "postgresql",
                    "error": s.get("error")}
        return _memory.status()

    def save(self, *, analysis_id: str, baseline_name: str,
             candidate_name: str, baseline_meta: dict,
             candidate_meta: dict, report_doc: dict, stats: dict,
             created_at: str, packages: dict) -> None:
        if self.db.available:
            self.db.save_analysis(
                analysis_id, baseline_name, candidate_name,
                baseline_meta, candidate_meta, report_doc, stats)
            self.db.save_packages(analysis_id, packages["baseline"],
                                  packages["candidate"])
            if not self.db.available:
                # connection dropped mid-request -> degrade
                _memory.save(self._record(
                    analysis_id, baseline_name, candidate_name,
                    baseline_meta, candidate_meta, report_doc, stats,
                    created_at), packages)
        else:
            _memory.save(self._record(
                analysis_id, baseline_name, candidate_name,
                baseline_meta, candidate_meta, report_doc, stats,
                created_at), packages)

    @staticmethod
    def _record(analysis_id, baseline_name, candidate_name, baseline_meta,
                candidate_meta, report_doc, stats, created_at):
        return {"id": analysis_id, "created_at": created_at,
                "baseline_name": baseline_name,
                "candidate_name": candidate_name,
                "baseline_meta": baseline_meta,
                "candidate_meta": candidate_meta,
                "report": report_doc, "stats": stats}

    def list_analyses(self, limit: int = 50):
        if self.db.available:
            return self.db.list_analyses(limit)
        return _memory.list_analyses(limit)

    def get(self, analysis_id: str) -> Optional[dict[str, Any]]:
        if self.db.available:
            return self.db.get_analysis(analysis_id)
        return _memory.get(analysis_id)

    def get_packages(self, analysis_id: str) -> Optional[dict]:
        if self.db.available:
            return self.db.get_packages(analysis_id)
        return _memory.get_packages(analysis_id)

    def delete(self, analysis_id: str) -> bool:
        if self.db.available:
            return self.db.delete_analysis(analysis_id)
        return _memory.delete(analysis_id)

    def save_coverage(self, *, analysis_id: str, side: str, doc: dict,
                      filename: str | None, created_at: str) -> None:
        if self.db.available:
            ok = self.db.save_coverage(analysis_id, side, doc, filename,
                                       created_at)
            if not ok and not self.db.available:
                _memory.save_coverage(analysis_id, side, doc, filename,
                                      created_at)
        else:
            _memory.save_coverage(analysis_id, side, doc, filename,
                                  created_at)

    def get_coverage(self, analysis_id: str, side: str):
        if self.db.available:
            return self.db.get_coverage(analysis_id, side)
        return _memory.get_coverage(analysis_id, side)

    def list_coverage(self, analysis_id: str):
        if self.db.available:
            return self.db.list_coverage(analysis_id)
        return _memory.list_coverage(analysis_id)

    def delete_coverage(self, analysis_id: str, side: str) -> bool:
        if self.db.available:
            return self.db.delete_coverage(analysis_id, side)
        return _memory.delete_coverage(analysis_id, side)


store = Store()
