"""ETL entry point: one full cycle  snapshot/extract -> transform -> load -> commit watermarks.

Usage:  python run_etl.py [cron|manual]      (default: cron)

Safe to call every minute: if the previous run is still going, this call exits immediately.
"""
import logging
import logging.handlers
import os
import sys
import time
import fcntl
import traceback
from datetime import date

from config import CONTROL_PATH, LOG_DIR, LOCK_PATH
from control import Control
from db import oltp_conn, dw_conn, dw_read_conn
from extract import extract
from transform import transform_all
from load import load_all
from metrics import Metrics

log = logging.getLogger("etl")


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "etl.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(h)


def run(trigger: str) -> None:
    ctl = Control(CONTROL_PATH)
    src = dw = dw_read = None
    run_id = None
    try:
        ctl.fail_stale_runs()                      # safe: we hold the lock, so nothing else is running
        run_id = ctl.start_run(trigger)
        m = Metrics(ctl, run_id)
        today = date.today()
        t0 = time.perf_counter()
        log.info("=== run %d started (%s) ===", run_id, trigger)

        state, fp_old = ctl.load_state(), ctl.load_product_fp()
        src, dw_read, dw = oltp_conn(), dw_read_conn(), dw_conn()

        raw, new_state, fp_updates, manifest = extract(src, dw_read, state, fp_old, m, today)
        ctl.add_manifest(run_id, manifest)

        clean, rejects = transform_all(raw, today, m)
        rejects += load_all(dw, clean, today, m)   # single DW transaction, commits inside

        if rejects:
            ctl.add_rejects(run_id, rejects)
        ctl.apply_state(new_state, fp_updates)     # ONLY after the DW commit succeeded
        ctl.finish_run(run_id, "Success", None)
        log.info("=== run %d OK in %.2fs (rejected=%d) ===", run_id, time.perf_counter() - t0, len(rejects))
    except Exception as e:
        log.error("run failed:\n%s", traceback.format_exc())
        try:
            if dw:
                dw.rollback()
        except Exception:
            pass
        if run_id:
            ctl.finish_run(run_id, "Failed", f"{type(e).__name__}: {e}"[:2000])
        raise
    finally:
        for c in (src, dw_read, dw):
            try:
                if c:
                    c.close()
            except Exception:
                pass
        ctl.close()


def main() -> int:
    setup_logging()
    trigger = "Manual" if (len(sys.argv) > 1 and sys.argv[1].lower() == "manual") else "Cron"
    os.makedirs(os.path.dirname(LOCK_PATH) or ".", exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("previous run still in progress -> skipping this tick")
        return 0
    try:
        run(trigger)
        return 0
    except Exception:
        return 1
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    sys.exit(main())