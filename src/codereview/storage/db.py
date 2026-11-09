"""SQLite persistence.

One connection guarded by a lock: the workload is small (a few PRs per minute)
and SQLite's WAL mode keeps readers from blocking the review worker.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from codereview.models import ReviewOutcome

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    delivery_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    installation_id INTEGER,
    action TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    posted INTEGER NOT NULL DEFAULT 0,
    review_id INTEGER,
    findings_json TEXT NOT NULL,
    errors_json TEXT NOT NULL,
    summary_body TEXT NOT NULL,
    comments_json TEXT NOT NULL,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    config_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS runs_pr ON runs(repo, pr_number, head_sha);

CREATE TABLE IF NOT EXISTS posted_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    review_id INTEGER,
    posted_at TEXT NOT NULL,
    UNIQUE(repo, pr_number, head_sha, fingerprint)
);

CREATE TABLE IF NOT EXISTS review_cache (
    cache_key TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings (
    content_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (content_hash, model)
);

CREATE TABLE IF NOT EXISTS chunk_cache (
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    blob_sha TEXT NOT NULL,
    chunks_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (repo, path, blob_sha)
);

CREATE TABLE IF NOT EXISTS repo_index_state (
    repo TEXT NOT NULL,
    ref TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (repo, ref)
);

CREATE TABLE IF NOT EXISTS pr_snapshots (
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    diff TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (repo, pr_number, head_sha)
);

CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    file TEXT,
    line INTEGER,
    issue TEXT NOT NULL,
    severity TEXT NOT NULL,
    labeler TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(repo, pr_number, head_sha, file, line, issue)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pack_vector(vector: Iterable[float]) -> bytes:
    return array("f", [float(x) for x in vector]).tobytes()


def _unpack_vector(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    return list(values)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._migrate_runs()
            self._conn.commit()

    def _migrate_runs(self) -> None:
        """Add columns that older databases predate."""
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "posted" not in columns:
            self._conn.execute("ALTER TABLE runs ADD COLUMN posted INTEGER NOT NULL DEFAULT 0")
        if "review_id" not in columns:
            self._conn.execute("ALTER TABLE runs ADD COLUMN review_id INTEGER")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- deliveries / jobs -------------------------------------------------

    def record_delivery(self, delivery_id: str, event: str) -> bool:
        """True if this delivery is new; False if already seen."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO deliveries(delivery_id, event, received_at) VALUES (?, ?, ?)",
                (delivery_id, event, _now()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def create_job(
        self,
        delivery_id: str,
        repo: str,
        pr_number: int,
        installation_id: int | None,
        action: str,
        head_sha: str,
    ) -> bool:
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (delivery_id, repo, pr_number, installation_id, action, head_sha, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (delivery_id, repo, pr_number, installation_id, action, head_sha, _now()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def pending_jobs(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status='pending' ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_job(self, delivery_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE delivery_id=? AND status='pending'",
                (_now(), delivery_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def finish_job(self, delivery_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, finished_at=?, error=? WHERE delivery_id=?",
                (status, _now(), (error or "")[:2000] or None, delivery_id),
            )
            self._conn.commit()

    def job(self, delivery_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            return dict(row) if row else None

    # --- runs --------------------------------------------------------------

    def save_run(self, outcome: ReviewOutcome, config_hash: str = "") -> None:
        data = outcome.to_dict()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO runs
                   (run_id, repo, pr_number, head_sha, mode, status, dry_run,
                    posted, review_id,
                    findings_json, errors_json, summary_body, comments_json,
                    cache_hit, llm_calls, prompt_tokens, completion_tokens,
                    duration_ms, config_hash, created_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data["run_id"], data["repo"], data["number"], data["head_sha"],
                    data["mode"], data["status"], int(data["dry_run"]),
                    int(data["posted"]), data["review_id"],
                    json.dumps(data["findings"], ensure_ascii=False),
                    json.dumps(data["errors"], ensure_ascii=False),
                    data["summary_body"],
                    json.dumps(data["inline_comments"], ensure_ascii=False),
                    int(data["cache_hit"]), data["llm_calls"],
                    data["prompt_tokens"], data["completion_tokens"],
                    data["duration_ms"], config_hash,
                    _now(), _now() if data["status"] in ("completed", "skipped", "failed") else None,
                ),
            )
            self._conn.commit()

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def list_runs(self, limit: int = 100, repo: str | None = None) -> list[dict]:
        with self._lock:
            if repo:
                rows = self._conn.execute(
                    "SELECT * FROM runs WHERE repo=? ORDER BY created_at DESC LIMIT ?",
                    (repo, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(row) for row in rows]

    # --- caches ------------------------------------------------------------

    def get_review_cache(self, cache_key: str) -> ReviewOutcome | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT outcome_json FROM review_cache WHERE cache_key=?", (cache_key,)
            ).fetchone()
        if not row:
            return None
        return ReviewOutcome.from_dict(json.loads(row["outcome_json"]))

    def set_review_cache(self, cache_key: str, outcome: ReviewOutcome) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO review_cache
                   (cache_key, repo, pr_number, head_sha, outcome_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    cache_key, outcome.repo, outcome.number, outcome.head_sha,
                    json.dumps(outcome.to_dict(), ensure_ascii=False), _now(),
                ),
            )
            self._conn.commit()

    def mark_posted(
        self, repo: str, number: int, head_sha: str, fingerprint: str, review_id: int | None
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO posted_reviews
                   (repo, pr_number, head_sha, fingerprint, review_id, posted_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (repo, number, head_sha, fingerprint, review_id, _now()),
            )
            self._conn.commit()

    def is_posted(self, repo: str, number: int, head_sha: str, fingerprint: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM posted_reviews
                   WHERE repo=? AND pr_number=? AND head_sha=? AND fingerprint=?""",
                (repo, number, head_sha, fingerprint),
            ).fetchone()
            return row is not None

    def embedding_get_hashes(
        self, model: str, hashes: list[str]
    ) -> dict[str, list[float]]:
        """Fetch vectors for many content hashes in one query."""
        if not hashes:
            return {}
        result: dict[str, list[float]] = {}
        with self._lock:
            # SQLite has a limit on bound variables; chunk at 500.
            for start in range(0, len(hashes), 500):
                batch = hashes[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = self._conn.execute(
                    f"SELECT content_hash, vector FROM embeddings "
                    f"WHERE model=? AND content_hash IN ({placeholders})",
                    [model, *batch],
                ).fetchall()
                for row in rows:
                    result[row["content_hash"]] = _unpack_vector(row["vector"])
        return result

    def embedding_put_many(
        self, entries: list[tuple[str, str, list[float]]]
    ) -> None:
        if not entries:
            return
        with self._lock:
            self._conn.executemany(
                """INSERT OR REPLACE INTO embeddings
                   (content_hash, model, dim, vector, created_at) VALUES (?, ?, ?, ?, ?)""",
                [
                    (content_hash, model, len(vector), _pack_vector(vector), _now())
                    for content_hash, model, vector in entries
                ],
            )
            self._conn.commit()

    def chunk_get(self, repo: str, path: str, blob_sha: str) -> list[dict] | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT chunks_json FROM chunk_cache
                   WHERE repo=? AND path=? AND blob_sha=?""",
                (repo, path, blob_sha),
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["chunks_json"])
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, list) else None

    def chunk_put_many(
        self, entries: list[tuple[str, str, str, list[dict]]]
    ) -> None:
        if not entries:
            return
        with self._lock:
            self._conn.executemany(
                """INSERT OR REPLACE INTO chunk_cache
                   (repo, path, blob_sha, chunks_json, created_at) VALUES (?, ?, ?, ?, ?)""",
                [
                    (repo, path, blob_sha, json.dumps(chunks, ensure_ascii=False), _now())
                    for repo, path, blob_sha, chunks in entries
                ],
            )
            self._conn.commit()

    def index_state_get(self, repo: str, ref: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM repo_index_state WHERE repo=? AND ref=?", (repo, ref)
            ).fetchone()
            return dict(row) if row else None

    def index_state_set(
        self, repo: str, ref: str, file_count: int, chunk_count: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO repo_index_state
                   (repo, ref, file_count, chunk_count, indexed_at) VALUES (?, ?, ?, ?, ?)""",
                (repo, ref, file_count, chunk_count, _now()),
            )
            self._conn.commit()

    # --- PR snapshots ------------------------------------------------------

    def save_snapshot(
        self, repo: str, number: int, head_sha: str, title: str, body: str, diff: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO pr_snapshots
                   (repo, pr_number, head_sha, title, body, diff, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (repo, number, head_sha, title, body, diff, _now()),
            )
            self._conn.commit()

    def get_snapshot(self, repo: str, number: int, head_sha: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM pr_snapshots
                   WHERE repo=? AND pr_number=? AND head_sha=?""",
                (repo, number, head_sha),
            ).fetchone()
            return dict(row) if row else None

    # --- labels ------------------------------------------------------------

    def add_labels(self, labels: list[dict]) -> int:
        inserted = 0
        with self._lock:
            for label in labels:
                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO labels
                       (repo, pr_number, head_sha, file, line, issue, severity, labeler, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        label["repo"], int(label["pr_number"]), label.get("head_sha", ""),
                        label.get("file"), label.get("line"), label["issue"],
                        label.get("severity", "medium"), label.get("labeler", ""), _now(),
                    ),
                )
                inserted += cur.rowcount
            self._conn.commit()
        return inserted

    def labels_all(self, repo: str | None = None) -> list[dict]:
        with self._lock:
            if repo:
                rows = self._conn.execute(
                    "SELECT * FROM labels WHERE repo=? ORDER BY pr_number, file, line", (repo,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM labels ORDER BY repo, pr_number, file, line"
                ).fetchall()
            return [dict(row) for row in rows]

    def stats(self) -> dict:
        with self._lock:
            def count(table: str) -> int:
                row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                return int(row["n"]) if row else 0

            return {
                "runs": count("runs"),
                "jobs_pending": self._conn.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE status='pending'"
                ).fetchone()["n"],
                "posted_reviews": count("posted_reviews"),
                "embeddings": count("embeddings"),
                "cached_chunks": count("chunk_cache"),
                "labels": count("labels"),
            }
