"""STEP 4: LOAD  (clean rows -> Data Warehouse).  ONE transaction: commit at the end, rollback on error.

Order (dimensions first so facts can store surrogate keys):
  1 static dimensions   2 carrier   3 type-1 dimensions (customer, seller, geography, coupon, tag)
  4 dim_product (SCD type 2)   5 bridge_product_tag   6 surrogate-key lookups   7 facts

WHY A SECOND CRON RUN NEVER DUPLICATES ANYTHING
  * every dimension has a UNIQUE natural key  -> INSERT ... ON DUPLICATE KEY UPDATE / INSERT IGNORE
  * every fact's primary key is the SOURCE id (order_item_id, payment_id, ...) -> same row = same key
  * identical re-loaded values change 0 rows (MySQL reports 0 affected rows for an unchanged upsert)
  * dim_product only gets a new version when a tracked attribute really changed
"""
import time
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from transform import reject

CHUNK = 500
FAR_FUTURE = date(9999, 12, 31)
EPOCH = date(1900, 1, 1)

STATIC_DIMS = {
    "dim_order_status":    ("status_name", ("Pending", "Processed", "Shipped", "Delivered", "Cancelled")),
    "dim_payment_method":  ("method_name", ("Credit Card", "Wallet", "Transfer")),
    "dim_payment_status":  ("status_name", ("Success", "Failed", "Pending")),
    "dim_shipment_status": ("status_name", ("Preparing", "In_Transit", "Delivered", "Returned")),
    "dim_return_status":   ("status_name", ("Requested", "Approved", "Rejected", "Refunded")),
}


# ----------------------------------------------------------------------------- helpers
def _upsert_sql(table, cols, keys):
    ph = ",".join(["%s"] * len(cols))
    upd = [c for c in cols if c not in keys]
    if not upd:
        return f"INSERT IGNORE INTO {table} ({','.join(cols)}) VALUES ({ph})"
    return (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph}) "
            f"ON DUPLICATE KEY UPDATE {','.join(f'{c}=VALUES({c})' for c in upd)}")


def _write(cur, sql, rows, size=1000):
    """executemany in chunks; returns the number of rows actually inserted/changed."""
    affected = 0
    for i in range(0, len(rows), size):
        cur.executemany(sql, rows[i:i + size])
        affected += cur.rowcount
    return affected


def _keymap(cur, table, nk, sk, ids):
    ids = sorted({i for i in ids if i is not None})
    out = {}
    for i in range(0, len(ids), CHUNK):
        ch = ids[i:i + CHUNK]
        cur.execute(f"SELECT {nk} AS n, {sk} AS s FROM {table} WHERE {nk} IN ({','.join(['%s'] * len(ch))})", ch)
        out.update({r["n"]: r["s"] for r in cur.fetchall()})
    return out


def _statusmap(cur, table, key_col, name_col):
    cur.execute(f"SELECT {name_col} AS n, {key_col} AS s FROM {table}")
    return {r["n"]: r["s"] for r in cur.fetchall()}


def _ids(clean, field, *names):
    return {r[field] for n in names for r in clean[n]}


class _Step:
    """tiny timer so each load step logs rows_in / affected / rejected / duration"""
    def __init__(self, m, table, rows_in, rej):
        self.m, self.table, self.n_in, self.rej = m, table, rows_in, rej
        self.before, self.t0 = len(rej), time.perf_counter()

    def done(self, affected):
        self.m.record("Load", self.table, self.n_in, affected, len(self.rej) - self.before,
                      time.perf_counter() - self.t0)


# ----------------------------------------------------------------------------- dimensions
def _load_static(cur, clean, m, rej):
    st = _Step(m, "static_dimensions", 0, rej)
    affected, n = 0, 0
    for table, (col, values) in STATIC_DIMS.items():
        affected += _write(cur, f"INSERT IGNORE INTO {table} ({col}) VALUES (%s)", [(v,) for v in values])
        n += len(values)
    carriers = sorted({s["carrier_name"] for s in clean["shipment"]})
    affected += _write(cur, "INSERT IGNORE INTO dim_carrier (carrier_name) VALUES (%s)", [(c,) for c in carriers])
    st.n_in = n + len(carriers)
    st.done(affected)


def _load_type1(cur, clean, m, rej):
    specs = [
        ("dim_customer", "customer", ["user_id", "email", "full_name", "registered_at"], ["user_id"]),
        ("dim_seller", "seller", ["user_id", "email", "full_name"], ["user_id"]),
        ("dim_geography", "geography", ["address_id", "city", "state", "postal_code", "country"], ["address_id"]),
        ("dim_coupon", "coupon", ["coupon_id", "code", "discount_type", "discount_value", "valid_from", "valid_until"],
         ["coupon_id"]),
        ("dim_tag", "tag", ["tag_id", "name"], ["tag_id"]),
    ]
    for table, src, cols, keys in specs:
        rows = [tuple(r[c] for c in cols) for r in clean[src]]
        st = _Step(m, table, len(rows), rej)
        st.done(_write(cur, _upsert_sql(table, cols, keys), rows) if rows else 0)


def _load_product_scd2(cur, clean, run_date, m, rej):
    products = clean["product"]
    st = _Step(m, "dim_product(SCD2)", len(products), rej)
    if not products:
        st.done(0); return
    ids = [p["product_id"] for p in products]
    current = {}
    for i in range(0, len(ids), CHUNK):
        ch = ids[i:i + CHUNK]
        cur.execute("SELECT product_key,product_id,name,description,category_level1,category_level2,"
                    "category_level3,current_price,effective_from FROM dim_product "
                    f"WHERE is_current=1 AND product_id IN ({','.join(['%s'] * len(ch))})", ch)
        current.update({r["product_id"]: r for r in cur.fetchall()})

    tracked = ("name", "description", "category_level1", "category_level2", "category_level3")
    ins_sql = ("INSERT INTO dim_product (product_id,name,description,category_level1,category_level2,category_level3,"
               "current_price,effective_from,effective_to,is_current) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1)")
    changed = 0
    for p in products:
        old = current.get(p["product_id"])
        vals = (p["product_id"], p["name"], p["description"], p["category_level1"], p["category_level2"],
                p["category_level3"], p["current_price"])
        if old is None:                                             # brand new product: first version
            cur.execute(ins_sql, vals + (EPOCH, FAR_FUTURE)); changed += 1
            continue
        same = (all((old[c] or "") == (p[c] or "") for c in tracked)
                and Decimal(str(old["current_price"])) == p["current_price"])
        if same:
            continue                                                # nothing changed -> NO new version
        if old["effective_from"] == run_date:                       # 2nd change on the same day: fix in place
            cur.execute("UPDATE dim_product SET name=%s,description=%s,category_level1=%s,category_level2=%s,"
                        "category_level3=%s,current_price=%s WHERE product_key=%s",
                        (p["name"], p["description"], p["category_level1"], p["category_level2"],
                         p["category_level3"], p["current_price"], old["product_key"]))
        else:                                                       # close old version, open a new one
            cur.execute("UPDATE dim_product SET effective_to=%s,is_current=0 WHERE product_key=%s",
                        (run_date - timedelta(days=1), old["product_key"]))
            cur.execute(ins_sql, vals + (run_date, FAR_FUTURE))
        changed += 1
    st.done(changed)


def _load_bridge(cur, clean, m, rej):
    pairs = clean["product_tag"]
    st = _Step(m, "bridge_product_tag", len(pairs), rej)
    if not pairs:
        st.done(0); return
    cur_ids = sorted({p["product_id"] for p in pairs})
    prod = {}
    for i in range(0, len(cur_ids), CHUNK):
        ch = cur_ids[i:i + CHUNK]
        cur.execute(f"SELECT product_id,product_key FROM dim_product WHERE is_current=1 AND product_id IN "
                    f"({','.join(['%s'] * len(ch))})", ch)
        prod.update({r["product_id"]: r["product_key"] for r in cur.fetchall()})
    tags = _keymap(cur, "dim_tag", "tag_id", "tag_key", {p["tag_id"] for p in pairs})
    rows = []
    for p in pairs:
        pk, tk = prod.get(p["product_id"]), tags.get(p["tag_id"])
        if pk is None or tk is None:
            reject(rej, "product_tags", f"{p['product_id']}-{p['tag_id']}", "orphan_key", "product or tag not in DW")
            continue
        rows.append((pk, tk))
    st.done(_write(cur, "INSERT IGNORE INTO bridge_product_tag (product_key,tag_key) VALUES (%s,%s)", rows) if rows else 0)


# ----------------------------------------------------------------------------- facts
def _product_versions(cur, ids):
    ids = sorted(set(ids))
    out = defaultdict(list)
    for i in range(0, len(ids), CHUNK):
        ch = ids[i:i + CHUNK]
        cur.execute("SELECT product_key,product_id,effective_from,effective_to FROM dim_product "
                    f"WHERE product_id IN ({','.join(['%s'] * len(ch))})", ch)
        for r in cur.fetchall():
            out[r["product_id"]].append((r["product_key"], r["effective_from"], r["effective_to"]))
    return out


def _bad(rej, table, sid, **keys):
    missing = [k for k, v in keys.items() if v is None]
    if missing:
        reject(rej, table, sid, "orphan_key", "no dimension row for: " + ", ".join(missing))
    return bool(missing)


def _load_facts(cur, clean, m, rej):
    cur.execute("SELECT MIN(date_key) AS lo, MAX(date_key) AS hi FROM dim_date")
    rng = cur.fetchone()
    in_range = lambda k: k is None or rng["lo"] <= k <= rng["hi"]          # noqa: E731

    cust = _keymap(cur, "dim_customer", "user_id", "customer_key",
                   _ids(clean, "customer_user_id", "sales", "order_coupon", "payment", "shipment", "return",
                        "review", "cart_item"))
    sell = _keymap(cur, "dim_seller", "user_id", "seller_key", _ids(clean, "seller_user_id", "sales", "return", "inventory"))
    geo = _keymap(cur, "dim_geography", "address_id", "geo_key", _ids(clean, "address_id", "sales", "shipment"))
    coup = _keymap(cur, "dim_coupon", "coupon_id", "coupon_key", _ids(clean, "coupon_id", "order_coupon"))
    versions = _product_versions(cur, _ids(clean, "product_id", "sales", "return", "review", "inventory", "cart_item"))
    ostat = _statusmap(cur, "dim_order_status", "order_status_key", "status_name")
    pmeth = _statusmap(cur, "dim_payment_method", "payment_method_key", "method_name")
    pstat = _statusmap(cur, "dim_payment_status", "payment_status_key", "status_name")
    sstat = _statusmap(cur, "dim_shipment_status", "shipment_status_key", "status_name")
    rstat = _statusmap(cur, "dim_return_status", "return_status_key", "status_name")
    carr = _statusmap(cur, "dim_carrier", "carrier_key", "carrier_name")

    def pkey(pid, on_date):                  # product version that was valid on that date
        for key, f, t in versions.get(pid, ()):
            if f <= on_date <= t:
                return key
        return None

    def run(table, name, cols, keys, src, build):
        st = _Step(m, table, len(src), rej)
        rows = [r for r in (build(x) for x in src) if r is not None]
        st.done(_write(cur, _upsert_sql(table, cols, keys), rows) if rows else 0)

    def date_ok(table, sid, *dks):
        if all(in_range(k) for k in dks):
            return True
        reject(rej, table, sid, "date_not_in_dim", "date outside dim_date range"); return False

    # ---- fact_sales ---------------------------------------------------------------------------
    def b_sales(r):
        ck, sk, gk, ok = cust.get(r["customer_user_id"]), sell.get(r["seller_user_id"]), geo.get(r["address_id"]), ostat.get(r["order_status"])
        pk = pkey(r["product_id"], r["order_date"])
        if _bad(rej, "order_items", r["order_item_id"], customer=ck, seller=sk, geography=gk, order_status=ok,
                product=pk) or not date_ok("order_items", r["order_item_id"], r["date_key"]):
            return None
        return (r["order_item_id"], r["order_id"], r["date_key"], ck, sk, pk, gk, ok, r["quantity"],
                r["unit_price"], r["subtotal"], r["allocated_discount"])
    run("fact_sales", "sales",
        ["order_item_id", "order_id", "date_key", "customer_key", "seller_key", "product_key", "ship_to_geo_key",
         "order_status_key", "quantity", "unit_price", "subtotal", "allocated_discount"],
        ["order_item_id"], clean["sales"], b_sales)

    # ---- fact_order_coupon ----------------------------------------------------------------------
    def b_oc(r):
        ck, cq = cust.get(r["customer_user_id"]), coup.get(r["coupon_id"])
        if _bad(rej, "order_coupons", f"{r['order_id']}-{r['coupon_id']}", customer=ck, coupon=cq) or \
                not date_ok("order_coupons", r["order_id"], r["date_key"]):
            return None
        return (r["order_id"], cq, r["date_key"], ck, r["discount_applied"])
    run("fact_order_coupon", "order_coupon", ["order_id", "coupon_key", "date_key", "customer_key", "discount_applied"],
        ["order_id", "coupon_key"], clean["order_coupon"], b_oc)

    # ---- fact_payment ---------------------------------------------------------------------------
    def b_pay(r):
        ck, mk, sk = cust.get(r["customer_user_id"]), pmeth.get(r["payment_method"]), pstat.get(r["payment_status"])
        if _bad(rej, "payments", r["payment_id"], customer=ck, payment_method=mk, payment_status=sk) or \
                not date_ok("payments", r["payment_id"], r["date_key"]):
            return None
        return (r["payment_id"], r["order_id"], r["provider_transaction_id"], r["date_key"], ck, mk, sk, r["amount"])
    run("fact_payment", "payment",
        ["payment_id", "order_id", "provider_transaction_id", "date_key", "customer_key", "payment_method_key",
         "payment_status_key", "amount"], ["payment_id"], clean["payment"], b_pay)

    # ---- fact_shipment (accumulating snapshot: the same row is updated as milestones are reached)
    def b_ship(r):
        ck, gk, cr, ss = cust.get(r["customer_user_id"]), geo.get(r["address_id"]), carr.get(r["carrier_name"]), sstat.get(r["shipment_status"])
        if _bad(rej, "shipment", r["shipment_id"], customer=ck, geography=gk, carrier=cr, shipment_status=ss) or \
                not date_ok("shipment", r["shipment_id"], r["shipped_date_key"], r["estimated_delivery_date_key"],
                            r["actual_delivery_date_key"]):
            return None
        return (r["shipment_id"], r["order_id"], r["tracking_number"], ck, gk, cr, ss, r["shipped_date_key"],
                r["estimated_delivery_date_key"], r["actual_delivery_date_key"], r["days_to_ship"],
                r["days_to_deliver"], r["days_late"])
    run("fact_shipment", "shipment",
        ["shipment_id", "order_id", "tracking_number", "customer_key", "ship_to_geo_key", "carrier_key",
         "shipment_status_key", "shipped_date_key", "estimated_delivery_date_key", "actual_delivery_date_key",
         "days_to_ship", "days_to_deliver", "days_late"], ["shipment_id"], clean["shipment"], b_ship)

    # ---- fact_return ----------------------------------------------------------------------------
    def b_ret(r):
        ck, sk, rs = cust.get(r["customer_user_id"]), sell.get(r["seller_user_id"]), rstat.get(r["return_status"])
        pk = pkey(r["product_id"], r["return_date"])
        if _bad(rej, "returns", r["return_id"], customer=ck, seller=sk, return_status=rs, product=pk) or \
                not date_ok("returns", r["return_id"], r["date_key"]):
            return None
        return (r["return_id"], r["order_item_id"], r["date_key"], ck, pk, sk, rs, r["reason"], r["refund_amount"])
    run("fact_return", "return",
        ["return_id", "order_item_id", "date_key", "customer_key", "product_key", "seller_key", "return_status_key",
         "reason", "refund_amount"], ["return_id"], clean["return"], b_ret)

    # ---- fact_review ----------------------------------------------------------------------------
    def b_rev(r):
        ck, pk = cust.get(r["customer_user_id"]), pkey(r["product_id"], r["review_date"])
        if _bad(rej, "product_reviews", r["review_id"], customer=ck, product=pk) or \
                not date_ok("product_reviews", r["review_id"], r["date_key"]):
            return None
        return (r["review_id"], r["order_item_id"], r["date_key"], ck, pk, r["rating"])
    run("fact_review", "review", ["review_id", "order_item_id", "date_key", "customer_key", "product_key", "rating"],
        ["review_id"], clean["review"], b_rev)

    # ---- fact_inventory_daily (periodic snapshot, unique per product per day) -------------------
    def b_inv(r):
        sk, pk = sell.get(r["seller_user_id"]), pkey(r["product_id"], r["snapshot_date"])
        if _bad(rej, "products", r["product_id"], seller=sk, product=pk) or \
                not date_ok("products", r["product_id"], r["date_key"]):
            return None
        return (r["date_key"], pk, sk, r["stock_quantity"])
    run("fact_inventory_daily", "inventory", ["date_key", "product_key", "seller_key", "stock_quantity"],
        ["date_key", "product_key"], clean["inventory"], b_inv)

    # ---- fact_cart_item -------------------------------------------------------------------------
    def b_cart(r):
        ck, pk = cust.get(r["customer_user_id"]), pkey(r["product_id"], r["added_date"])
        if _bad(rej, "cart_items", f"{r['cart_id']}-{r['product_id']}", customer=ck, product=pk) or \
                not date_ok("cart_items", f"{r['cart_id']}-{r['product_id']}", r["date_key"]):
            return None
        return (r["cart_id"], pk, r["date_key"], ck, r["quantity"])
    run("fact_cart_item", "cart_item", ["cart_id", "product_key", "date_key", "customer_key", "quantity"],
        ["cart_id", "product_key"], clean["cart_item"], b_cart)


# ----------------------------------------------------------------------------- driver
def load_all(dw, clean, run_date: date, m):
    """Loads everything in ONE transaction. Returns the list of load-time rejects (orphan keys)."""
    rej = []
    with dw.cursor() as cur:
        _load_static(cur, clean, m, rej)
        _load_type1(cur, clean, m, rej)
        _load_product_scd2(cur, clean, run_date, m, rej)
        _load_bridge(cur, clean, m, rej)
        _load_facts(cur, clean, m, rej)
    dw.commit()                      # <- the ONLY commit: all or nothing
    return rej
