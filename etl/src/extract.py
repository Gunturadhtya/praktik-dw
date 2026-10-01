"""STEP 1-2: SNAPSHOT + EXTRACT (incremental).

The goal is to NEVER re-read the whole source on a normal run:

  * Transactional tables use a high-water mark on the auto-increment id
    (orders, payments, shipment, returns, product_reviews) plus a refresh of the records the DW
    still holds as OPEN (e.g. an order that was 'Shipped' last time and is 'Delivered' now).
    The open ids are read from the DW itself, so no extra state is needed for that.
  * order_items / order_coupons are read only for the orders in the current batch.
  * cart_items uses a timestamp watermark (added_at) with a small look-back window.
  * users / addresses use an id watermark (new rows only).
  * products / categories / coupons / tags / product_tags: ONE cheap checksum query
    (1 row back). Rows are read only when the checksum differs from the last run.
  * Parent rows needed to enrich a batch (an order for a payment, a product's seller, ...) are
    fetched by primary key ("lookups"), never by scanning.

Everything runs inside ONE consistent-snapshot transaction on the OLTP.
The new watermarks are only RETURNED here; run_etl.py stores them AFTER the DW commit, so a failed
run never skips data.  And because every load is an idempotent upsert, re-reading a few rows is
harmless (no duplicates).
"""
import time

from config import BATCH_SIZE, IN_CHUNK, LOOKBACK_MINUTES

# statuses that can still change in the source -> these records are re-read until they are final
OPEN_ORDER = ("Pending", "Processed", "Shipped")
OPEN_PAYMENT = ("Pending",)
OPEN_SHIPMENT = ("Preparing", "In_Transit")
OPEN_RETURN = ("Requested", "Approved")

ID_TABLES = ("users", "addresses", "orders", "payments", "shipment", "returns", "product_reviews")


def _q(cur, sql, params=()):
    cur.execute(sql, params)
    return list(cur.fetchall())   # pymysql returns a tuple; callers concatenate lists


def _in(cur, sql, ids):
    """Run `sql` (containing the token {in}) for a set of ids, in chunks."""
    ids = sorted({i for i in ids if i is not None})
    out = []
    for i in range(0, len(ids), IN_CHUNK):
        chunk = ids[i:i + IN_CHUNK]
        out.extend(_q(cur, sql.replace("{in}", ",".join(["%s"] * len(chunk))), chunk))
    return out


def _checksum(cur, table, expr):
    """One-row change detector: row count + sum of per-row CRC32. No data rows are transferred."""
    r = _q(cur, f"SELECT COUNT(*) AS c, COALESCE(SUM(CRC32(CONCAT_WS('|',{expr}))),0) AS s FROM {table}")[0]
    return f"{r['c']}:{r['s']}"


def _dw_open_ids(dw, fact, id_col, dim, key_col, names):
    ph = ",".join(["%s"] * len(names))
    with dw.cursor() as c:
        c.execute(f"SELECT DISTINCT f.{id_col} AS id FROM {fact} f JOIN {dim} s ON s.{key_col}=f.{key_col}"
                  f" WHERE s.status_name IN ({ph})", names)
        return [r["id"] for r in c.fetchall()]


def _timed(m, name, fn):
    t0 = time.perf_counter()
    rows = fn()
    m.record("Extract", name, len(rows), len(rows), 0, time.perf_counter() - t0)
    return rows


def _incremental(cur, dw, state, ns, table, cols, open_spec):
    """new rows (id > watermark, max BATCH_SIZE) + records the DW still holds as open."""
    wm = int(state.get(f"wm:{table}", 0))
    new = _q(cur, f"SELECT {cols} FROM {table} WHERE id > %s ORDER BY id LIMIT %s", (wm, BATCH_SIZE))
    if new:
        ns[f"wm:{table}"] = new[-1]["id"]
    have = {r["id"] for r in new}
    open_ids = _dw_open_ids(dw, *open_spec) if open_spec else []
    extra = _in(cur, f"SELECT {cols} FROM {table} WHERE id IN ({{in}})", [i for i in open_ids if i not in have])
    return new + extra


def extract(src, dw, state, fp_old, m, today):
    """Returns (raw, new_state, product_fp_updates, manifest)."""
    raw, ns, fpu, manifest = {}, {}, {}, []
    wm = lambda t: int(state.get(f"wm:{t}", 0))  # noqa: E731

    with src.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        try:
            # ---- STEP 1: snapshot manifest (cheap indexed range counts) ----------------------
            for t in ID_TABLES:
                r = _q(cur, f"SELECT COUNT(*) AS c, COALESCE(MAX(id),0) AS mx FROM {t} WHERE id > %s", (wm(t),))[0]
                manifest.append((t, r["c"], r["mx"]))

            # ---- STEP 2: extract -------------------------------------------------------------
            # master data: new rows only
            raw["users"] = _timed(m, "users", lambda: _q(
                cur, "SELECT id,email,full_name,created_at FROM users WHERE id > %s ORDER BY id", (wm("users"),)))
            if raw["users"]:
                ns["wm:users"] = raw["users"][-1]["id"]
            raw["addresses"] = _timed(m, "addresses", lambda: _q(
                cur, "SELECT id,city,state,postal_code,country FROM addresses WHERE id > %s ORDER BY id",
                (wm("addresses"),)))
            if raw["addresses"]:
                ns["wm:addresses"] = raw["addresses"][-1]["id"]

            # products + categories: checksum first, rows only when something changed
            cat_sum = _checksum(cur, "categories", "id,parent_category_id,name")
            cats_changed = cat_sum != state.get("fp:categories")
            prod_sum = _checksum(cur, "products",
                                 "id,category_id,seller_id,current_price,stock_quantity,version,CRC32(name),CRC32(description)")
            inv_due = state.get("inv:date") != today.isoformat()   # daily inventory snapshot not yet taken
            raw["products"], raw["inventory"] = [], []
            if cats_changed or inv_due or prod_sum != state.get("fp:products"):
                scan = _timed(m, "products_fingerprint", lambda: _q(
                    cur, "SELECT id,category_id,seller_id,current_price,stock_quantity,version,"
                         "CRC32(name) AS n,CRC32(description) AS d FROM products"))
                fp_now = {r["id"]: f"{r['category_id']}|{r['seller_id']}|{r['current_price']}|"
                                   f"{r['stock_quantity']}|{r['version']}|{r['n']}|{r['d']}" for r in scan}
                changed = list(fp_now) if cats_changed else [p for p, fp in fp_now.items() if fp_old.get(p) != fp]
                raw["products"] = _timed(m, "products", lambda: _in(
                    cur, "SELECT id,category_id,seller_id,name,description,current_price,stock_quantity "
                         "FROM products WHERE id IN ({in})", changed))
                chosen = set(fp_now) if inv_due else set(changed)   # 1st run of the day = all products
                raw["inventory"] = [dict(product_id=r["id"], seller_id=r["seller_id"],
                                         stock_quantity=r["stock_quantity"]) for r in scan if r["id"] in chosen]
                fpu = {p: fp_now[p] for p in changed}
                ns["fp:products"] = prod_sum
                ns["inv:date"] = today.isoformat()
            raw["categories"] = []
            if cats_changed or raw["products"]:
                raw["categories"] = _timed(m, "categories", lambda: _q(
                    cur, "SELECT id,parent_category_id,name FROM categories"))
                ns["fp:categories"] = cat_sum

            # sellers = users referenced by changed products (read by primary key)
            have_users = {u["id"] for u in raw["users"]}
            raw["seller_users"] = _timed(m, "seller_users", lambda: _in(
                cur, "SELECT id,email,full_name,created_at FROM users WHERE id IN ({in})",
                {p["seller_id"] for p in raw["products"]} - have_users))

            # small reference tables: checksum, then full read only if changed
            for table, cols, expr in (
                    ("coupons", "id,code,discount_type,discount_value,valid_from,valid_until",
                     "id,code,discount_type,discount_value,valid_from,valid_until"),
                    ("tags", "id,name", "id,name")):
                s = _checksum(cur, table, expr)
                raw[table] = []
                if s != state.get(f"fp:{table}"):
                    raw[table] = _timed(m, table, lambda: _q(cur, f"SELECT {cols} FROM {table}"))
                    ns[f"fp:{table}"] = s
            s = _checksum(cur, "product_tags", "product_id,tag_id")
            raw["product_tags"] = []
            if s != state.get("fp:product_tags") or raw["products"]:   # new SCD2 versions need bridge rows
                raw["product_tags"] = _timed(m, "product_tags", lambda: _q(
                    cur, "SELECT product_id,tag_id FROM product_tags"))
                ns["fp:product_tags"] = s

            # transactions: new rows + still-open records
            raw["orders"] = _timed(m, "orders", lambda: _incremental(
                cur, dw, state, ns, "orders", "id,user_id,shipping_address_id,order_date,status",
                ("fact_sales", "order_id", "dim_order_status", "order_status_key", OPEN_ORDER)))
            order_ids = [o["id"] for o in raw["orders"]]
            raw["order_items"] = _timed(m, "order_items", lambda: _in(
                cur, "SELECT id,order_id,product_id,quantity,unit_price,subtotal FROM order_items "
                     "WHERE order_id IN ({in})", order_ids))
            raw["order_coupons"] = _timed(m, "order_coupons", lambda: _in(
                cur, "SELECT order_id,coupon_id,discount_applied FROM order_coupons WHERE order_id IN ({in})",
                order_ids))
            raw["payments"] = _timed(m, "payments", lambda: _incremental(
                cur, dw, state, ns, "payments",
                "id,order_id,payment_method,provider_transaction_id,amount,payment_date,status",
                ("fact_payment", "payment_id", "dim_payment_status", "payment_status_key", OPEN_PAYMENT)))
            raw["shipment"] = _timed(m, "shipment", lambda: _incremental(
                cur, dw, state, ns, "shipment",
                "id,order_id,carrier_name,tracking_number,shipped_date,estimated_delivery_date,"
                "actual_delivery_date,status",
                ("fact_shipment", "shipment_id", "dim_shipment_status", "shipment_status_key", OPEN_SHIPMENT)))
            raw["returns"] = _timed(m, "returns", lambda: _incremental(
                cur, dw, state, ns, "returns", "id,order_item_id,reason,return_date,refund_amount,status",
                ("fact_return", "return_id", "dim_return_status", "return_status_key", OPEN_RETURN)))
            raw["reviews"] = _timed(m, "product_reviews", lambda: _incremental(
                cur, dw, state, ns, "product_reviews", "id,user_id,product_id,order_item_id,rating,created_at", None))

            ts = state.get("ts:cart_items")
            if ts:
                raw["cart_items"] = _timed(m, "cart_items", lambda: _q(
                    cur, "SELECT cart_id,product_id,quantity,added_at FROM cart_items "
                         "WHERE added_at >= DATE_SUB(%s, INTERVAL %s MINUTE) ORDER BY added_at", (ts, LOOKBACK_MINUTES)))
            else:
                raw["cart_items"] = _timed(m, "cart_items", lambda: _q(
                    cur, "SELECT cart_id,product_id,quantity,added_at FROM cart_items ORDER BY added_at"))
            stamps = [r["added_at"] for r in raw["cart_items"] if r["added_at"]]
            if stamps:
                ns["ts:cart_items"] = max(stamps).strftime("%Y-%m-%d %H:%M:%S")

            # ---- lookups: parent rows of this batch, fetched by primary key -------------------
            have_items = {i["id"] for i in raw["order_items"]}
            raw["lk_items"] = _timed(m, "lookup_order_items", lambda: _in(
                cur, "SELECT id,order_id,product_id,quantity,subtotal FROM order_items WHERE id IN ({in})",
                {r["order_item_id"] for r in raw["returns"]} - have_items))
            have_orders = set(order_ids)
            need_orders = ({p["order_id"] for p in raw["payments"]} | {s["order_id"] for s in raw["shipment"]}
                           | {i["order_id"] for i in raw["lk_items"]}) - have_orders
            raw["lk_orders"] = _timed(m, "lookup_orders", lambda: _in(
                cur, "SELECT id,user_id,shipping_address_id,order_date FROM orders WHERE id IN ({in})", need_orders))
            have_products = {p["id"] for p in raw["products"]}
            need_products = ({i["product_id"] for i in raw["order_items"]}
                             | {i["product_id"] for i in raw["lk_items"]}) - have_products
            raw["lk_products"] = _timed(m, "lookup_products", lambda: _in(
                cur, "SELECT id,seller_id FROM products WHERE id IN ({in})", need_products))
            raw["lk_carts"] = _timed(m, "lookup_carts", lambda: _in(
                cur, "SELECT id,user_id FROM carts WHERE id IN ({in})", {c["cart_id"] for c in raw["cart_items"]}))
        finally:
            cur.execute("ROLLBACK")   # read-only: just ends the snapshot
    return raw, ns, fpu, manifest