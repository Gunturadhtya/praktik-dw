"""Permanent control store (SQLite): run log, metrics, rejects, snapshot manifest and the
incremental-extraction state (watermarks). Lives on a Docker volume, so it survives restarts."""
import os
import sqlite3

DDL = """
CREATE TABLE IF NOT EXISTS etl_run_log (
  run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  trigger_type  TEXT NOT NULL CHECK (trigger_type IN ('Manual','Cron')),
  started_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  finished_at   TEXT,
  status        TEXT NOT NULL DEFAULT 'Running' CHECK (status IN ('Running','Success','Failed')),
  error_message TEXT
);
CREATE TABLE IF NOT EXISTS etl_table_metrics (
  metric_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id         INTEGER NOT NULL REFERENCES etl_run_log(run_id),
  stage          TEXT NOT NULL CHECK (stage IN ('Extract','Transform','Load')),
  table_name     TEXT NOT NULL,
  rows_in        INTEGER NOT NULL DEFAULT 0,
  rows_out       INTEGER NOT NULL DEFAULT 0,
  rows_rejected  INTEGER NOT NULL DEFAULT 0,
  duration_sec   REAL NOT NULL DEFAULT 0,
  throughput_rps REAL,
  logged_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS rejected_rows (
  reject_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       INTEGER NOT NULL REFERENCES etl_run_log(run_id),
  source_table TEXT NOT NULL,
  source_id    TEXT,
  rule_name    TEXT NOT NULL,
  reason       TEXT,
  rejected_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS transform_test_result (
  test_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id         INTEGER,
  rule_name      TEXT NOT NULL,
  before_value   TEXT,
  expected_value TEXT,
  actual_value   TEXT,
  status         TEXT NOT NULL CHECK (status IN ('PASS','FAIL')),
  tested_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS etl_snapshot_manifest (
  run_id       INTEGER NOT NULL REFERENCES etl_run_log(run_id),
  source_table TEXT NOT NULL,
  pending_rows INTEGER NOT NULL,
  max_id       INTEGER,
  captured_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (run_id, source_table)
);
CREATE TABLE IF NOT EXISTS etl_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS etl_product_fp (
  product_id INTEGER PRIMARY KEY,
  fp         TEXT NOT NULL
);
"""


class Control:
    def __init__(self, path: str):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.c = sqlite3.connect(path, timeout=30)
        self.c.execute("PRAGMA foreign_keys = ON")
        self.c.executescript(DDL)
        self.c.commit()

    # ---- run log -------------------------------------------------------
    def fail_stale_runs(self):
        """Called only after the run lock is held: any 'Running' row is a crashed earlier run."""
        self.c.execute(
            "UPDATE etl_run_log SET status='Failed', finished_at=datetime('now','localtime'),"
            " error_message='run did not finish (container stopped or crashed)' WHERE status='Running'")
        self.c.commit()

    def start_run(self, trigger: str) -> int:
        cur = self.c.execute("INSERT INTO etl_run_log (trigger_type) VALUES (?)", (trigger,))
        self.c.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, error: str | None):
        self.c.execute(
            "UPDATE etl_run_log SET finished_at=datetime('now','localtime'), status=?, error_message=?"
            " WHERE run_id=?", (status, error, run_id))
        self.c.commit()

    # ---- metrics / rejects / manifest ----------------------------------
    def add_metric(self, run_id, stage, table, rows_in, rows_out, rejected, seconds, throughput):
        self.c.execute(
            "INSERT INTO etl_table_metrics (run_id,stage,table_name,rows_in,rows_out,rows_rejected,"
            "duration_sec,throughput_rps) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, stage, table, rows_in, rows_out, rejected, seconds, throughput))
        self.c.commit()

    def add_rejects(self, run_id, rejects):
        self.c.executemany(
            "INSERT INTO rejected_rows (run_id,source_table,source_id,rule_name,reason) VALUES (?,?,?,?,?)",
            [(run_id, r["table"], r["source_id"], r["rule"], r["reason"]) for r in rejects])
        self.c.commit()

    def add_manifest(self, run_id, rows):
        self.c.executemany(
            "INSERT OR REPLACE INTO etl_snapshot_manifest (run_id,source_table,pending_rows,max_id)"
            " VALUES (?,?,?,?)", [(run_id, t, int(n or 0), int(mx or 0)) for t, n, mx in rows])  # MySQL returns Decimal
        self.c.commit()

    # ---- incremental state ---------------------------------------------
    def load_state(self) -> dict:
        return dict(self.c.execute("SELECT key, value FROM etl_state").fetchall())

    def load_product_fp(self) -> dict:
        return {pid: fp for pid, fp in self.c.execute("SELECT product_id, fp FROM etl_product_fp")}

    def apply_state(self, state_updates: dict, fp_updates: dict):
        """Advance watermarks. Called ONLY after the DW transaction has been committed."""
        with self.c:
            self.c.executemany("INSERT OR REPLACE INTO etl_state (key,value) VALUES (?,?)",
                               [(k, str(v)) for k, v in state_updates.items()])
            self.c.executemany("INSERT OR REPLACE INTO etl_product_fp (product_id,fp) VALUES (?,?)",
                               list(fp_updates.items()))

    def close(self):
        self.c.close()