"""Per-table rows / duration / throughput, written to the control DB and to the log."""
import logging

log = logging.getLogger("etl")


class Metrics:
    def __init__(self, control, run_id: int):
        self.control, self.run_id = control, run_id
        self.totals = {"Extract": [0, 0.0], "Transform": [0, 0.0], "Load": [0, 0.0]}

    def record(self, stage, table, rows_in, rows_out, rejected, seconds):
        # throughput = rows processed (rows_in) per second
        tp = round(rows_in / seconds, 2) if seconds > 0 else None
        self.control.add_metric(self.run_id, stage, table, rows_in, rows_out, rejected,
                                round(seconds, 4), tp)
        self.totals[stage][0] += rows_in
        self.totals[stage][1] += seconds
        log.info("[%s] %-22s in=%-6d out=%-6d rejected=%-4d %.3fs  %s rows/s",
                 stage.upper(), table, rows_in, rows_out, rejected, seconds, tp)
