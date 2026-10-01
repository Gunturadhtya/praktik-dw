"""Connections to the two external databases (OLTP source, DW target)."""
import pymysql
import pymysql.cursors

from config import oltp_cfg, dw_cfg


def oltp_conn():
    # autocommit ON: the extract opens its own consistent-snapshot transaction explicitly.
    return pymysql.connect(**oltp_cfg(), cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def dw_read_conn():
    # separate read-only-use connection (used by extract to find still-open records)
    return pymysql.connect(**dw_cfg(), cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def dw_conn():
    # autocommit OFF: the whole load is ONE transaction (commit at the end, rollback on error)
    return pymysql.connect(**dw_cfg(), cursorclass=pymysql.cursors.DictCursor, autocommit=False)
