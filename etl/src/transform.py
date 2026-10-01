"""STEP 3: TRANSFORM  (raw OLTP rows -> clean, load-ready rows).

Rule types implemented (each is testable, see test_transform.py):
  type cast        : timestamps -> date_key (yyyymmdd), money -> Decimal(2 places)
  null handling    : NULL text -> '' / 'N/A' / 'Unknown'; NULL optional dates stay NULL
  deduplication    : first row per natural key wins, the rest are rejected ('dedup')
  mapping          : status / method text -> canonical dimension label
  normalisation    : trim + collapse spaces, smart title-case for city/state/country/carrier
  business rules   : subtotal = qty*unit_price, rating 1..5, refund <= item subtotal,
                     delivered date >= shipped date, valid coupon window, e-mail format, ...
A row that breaks a rule is NEVER silently dropped: it goes to `rejects` with the rule + reason.
"""
import time
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

ORDER_STATUS = ("Pending", "Processed", "Shipped", "Delivered", "Cancelled")
PAYMENT_METHOD = ("Credit Card", "Wallet", "Transfer")
PAYMENT_STATUS = ("Success", "Failed", "Pending")
SHIPMENT_STATUS = ("Preparing", "In_Transit", "Delivered", "Returned")
RETURN_STATUS = ("Requested", "Approved", "Rejected", "Refunded")
DISCOUNT_TYPE = ("Percentage", "Fixed")
CENT = Decimal("0.01")


# ----------------------------------------------------------------------------- helpers
def norm_text(v) -> str:
    """trim + collapse inner whitespace; NULL -> ''"""
    return " ".join(str(v).split()) if v is not None else ""


def smart_title(v) -> str:
    """'banjarmasin'/'BANJARMASIN' -> 'Banjarmasin'; short acronyms (USA, DHL, JNE) are kept."""
    s = norm_text(v)
    if len(s) <= 3 and s.isupper():
        return s
    if s.islower() or s.isupper():
        return s.title()
    return s


def canon(v, allowed):
    """mapping rule: any spelling/case -> canonical label, or None if it is not a known value"""
    return {a.lower(): a for a in allowed}.get(norm_text(v).lower())


def to_date(v):
    if v is None:
        return None
    return v.date() if isinstance(v, datetime) else v


def date_key(v):
    d = to_date(v)
    return int(d.strftime("%Y%m%d")) if d else None


def money(v):
    try:
        return Decimal(str(v)).quantize(CENT)
    except (InvalidOperation, ValueError, TypeError):
        return None


def reject(rej, table, sid, rule, reason):
    rej.append(dict(table=table, source_id=str(sid), rule=rule, reason=reason))


# ----------------------------------------------------------------------------- dimensions
def t_customer(raw, rej):
    out, seen = [], set()
    for u in raw["users"]:
        if u["id"] in seen:
            reject(rej, "users", u["id"], "dedup", "duplicate user id"); continue
        email = norm_text(u["email"]).lower()
        if "@" not in email:
            reject(rej, "users", u["id"], "email_format", f"invalid e-mail '{email}'"); continue
        seen.add(u["id"])
        out.append(dict(user_id=u["id"], email=email, full_name=norm_text(u["full_name"]) or "Unknown",
                        registered_at=u["created_at"]))
    return out


def t_seller(raw, rej):
    seller_ids = {p["seller_id"] for p in raw["products"]}
    pool = {u["id"]: u for u in raw["seller_users"]}
    pool.update({u["id"]: u for u in raw["users"]})
    out = []
    for sid in sorted(seller_ids):
        u = pool.get(sid)
        if u is None:
            reject(rej, "products", sid, "orphan", "seller user not found"); continue
        out.append(dict(user_id=sid, email=norm_text(u["email"]).lower(),
                        full_name=norm_text(u["full_name"]) or "Unknown"))
    return out


def t_geography(raw, rej):
    out, seen = [], set()
    for a in raw["addresses"]:
        if a["id"] in seen:
            reject(rej, "addresses", a["id"], "dedup", "duplicate address id"); continue
        seen.add(a["id"])
        out.append(dict(address_id=a["id"], city=smart_title(a["city"]), state=smart_title(a["state"]),
                        postal_code=norm_text(a["postal_code"]), country=smart_title(a["country"])))
    return out


def _cat_path(cid, cats):
    path, seen = [], set()
    while cid is not None and cid in cats and cid not in seen:
        seen.add(cid)
        parent, name = cats[cid]
        path.append(norm_text(name))
        cid = parent
    return list(reversed(path))          # root first


def t_product(raw, rej):
    cats = {c["id"]: (c["parent_category_id"], c["name"]) for c in raw["categories"]}
    out, seen = [], set()
    for p in raw["products"]:
        if p["id"] in seen:
            reject(rej, "products", p["id"], "dedup", "duplicate product id"); continue
        price = money(p["current_price"])
        if price is None or price < 0:
            reject(rej, "products", p["id"], "price_rule", f"invalid price {p['current_price']}"); continue
        seen.add(p["id"])
        levels = (_cat_path(p["category_id"], cats) + ["N/A"] * 3)[:3]      # null handling for missing levels
        out.append(dict(product_id=p["id"], name=norm_text(p["name"]), description=norm_text(p["description"]),
                        category_level1=levels[0], category_level2=levels[1], category_level3=levels[2],
                        current_price=price))
    return out


def t_coupon(raw, rej):
    out, seen = [], set()
    for c in raw["coupons"]:
        if c["id"] in seen:
            reject(rej, "coupons", c["id"], "dedup", "duplicate coupon id"); continue
        dtype, val = canon(c["discount_type"], DISCOUNT_TYPE), money(c["discount_value"])
        if dtype is None or val is None or val < 0:
            reject(rej, "coupons", c["id"], "coupon_rule", "invalid discount type/value"); continue
        if c["valid_from"] and c["valid_until"] and c["valid_until"] < c["valid_from"]:
            reject(rej, "coupons", c["id"], "coupon_window", "valid_until before valid_from"); continue
        seen.add(c["id"])
        out.append(dict(coupon_id=c["id"], code=norm_text(c["code"]).upper(), discount_type=dtype,
                        discount_value=val, valid_from=c["valid_from"], valid_until=c["valid_until"]))
    return out


def t_tag(raw, rej):
    out, seen = [], set()
    for t in raw["tags"]:
        if t["id"] in seen:
            reject(rej, "tags", t["id"], "dedup", "duplicate tag id"); continue
        seen.add(t["id"])
        out.append(dict(tag_id=t["id"], name=norm_text(t["name"]).lower()))
    return out


def t_product_tag(raw, rej):
    out, seen = [], set()
    for r in raw["product_tags"]:
        k = (r["product_id"], r["tag_id"])
        if k in seen:
            reject(rej, "product_tags", f"{k[0]}-{k[1]}", "dedup", "duplicate pair"); continue
        seen.add(k)
        out.append(dict(product_id=k[0], tag_id=k[1]))
    return out


# ----------------------------------------------------------------------------- facts
def _ctx(raw):
    orders = {o["id"]: o for o in raw["lk_orders"]}
    orders.update({o["id"]: o for o in raw["orders"]})
    items = {i["id"]: i for i in raw["lk_items"]}
    items.update({i["id"]: i for i in raw["order_items"]})
    seller = {p["id"]: p["seller_id"] for p in raw["lk_products"]}
    seller.update({p["id"]: p["seller_id"] for p in raw["products"]})
    carts = {c["id"]: c["user_id"] for c in raw["lk_carts"]}
    return dict(orders=orders, items=items, seller=seller, carts=carts)


def t_sales(raw, ctx, rej):
    order_disc = defaultdict(Decimal)
    for oc in raw["order_coupons"]:
        order_disc[oc["order_id"]] += money(oc["discount_applied"]) or Decimal("0")
    seen, good = set(), []
    for it in raw["order_items"]:
        iid = it["id"]
        if iid in seen:
            reject(rej, "order_items", iid, "dedup", "duplicate order_item id"); continue
        seen.add(iid)
        o, seller = ctx["orders"].get(it["order_id"]), ctx["seller"].get(it["product_id"])
        if o is None or seller is None:
            reject(rej, "order_items", iid, "orphan", "order or product not found"); continue
        up, sub, status = money(it["unit_price"]), money(it["subtotal"]), canon(o["status"], ORDER_STATUS)
        dk = date_key(o["order_date"])
        if up is None or sub is None or it["quantity"] is None or status is None or dk is None:
            reject(rej, "order_items", iid, "type_cast", "unparsable price/quantity/status/date"); continue
        if sub != (up * it["quantity"]).quantize(CENT):
            reject(rej, "order_items", iid, "subtotal_rule", f"subtotal {sub} != {it['quantity']} x {up}"); continue
        good.append(dict(order_item_id=iid, order_id=o["id"], date_key=dk, order_date=to_date(o["order_date"]),
                         customer_user_id=o["user_id"], seller_user_id=seller, product_id=it["product_id"],
                         address_id=o["shipping_address_id"], order_status=status, quantity=it["quantity"],
                         unit_price=up, subtotal=sub, allocated_discount=Decimal("0.00")))
    # allocate the order-level discount over its lines, proportional to subtotal;
    # the last line takes the rounding remainder so the lines add up exactly
    by_order = defaultdict(list)
    for g in good:
        by_order[g["order_id"]].append(g)
    for oid, lines in by_order.items():
        disc = order_disc.get(oid, Decimal("0"))
        total = sum(l["subtotal"] for l in lines)
        if disc == 0 or total == 0:
            continue
        used = Decimal("0")
        for l in lines[:-1]:
            l["allocated_discount"] = (disc * l["subtotal"] / total).quantize(CENT)
            used += l["allocated_discount"]
        lines[-1]["allocated_discount"] = (disc - used).quantize(CENT)
    return good


def t_order_coupon(raw, ctx, rej):
    out, seen = [], set()
    for oc in raw["order_coupons"]:
        k = (oc["order_id"], oc["coupon_id"])
        if k in seen:
            reject(rej, "order_coupons", f"{k[0]}-{k[1]}", "dedup", "duplicate pair"); continue
        seen.add(k)
        o, amt = ctx["orders"].get(oc["order_id"]), money(oc["discount_applied"])
        if o is None or amt is None or amt < 0 or date_key(o["order_date"]) is None:
            reject(rej, "order_coupons", f"{k[0]}-{k[1]}", "orphan_or_amount", "order missing or bad amount"); continue
        out.append(dict(order_id=k[0], coupon_id=k[1], date_key=date_key(o["order_date"]),
                        customer_user_id=o["user_id"], discount_applied=amt))
    return out


def t_payment(raw, ctx, rej):
    out, seen = [], set()
    for p in raw["payments"]:
        if p["id"] in seen:
            reject(rej, "payments", p["id"], "dedup", "duplicate payment id"); continue
        seen.add(p["id"])
        o = ctx["orders"].get(p["order_id"])
        method, status = canon(p["payment_method"], PAYMENT_METHOD), canon(p["status"], PAYMENT_STATUS)
        amt, dk, txn = money(p["amount"]), date_key(p["payment_date"]), norm_text(p["provider_transaction_id"])
        if o is None:
            reject(rej, "payments", p["id"], "orphan", "order not found"); continue
        if method is None or status is None:
            reject(rej, "payments", p["id"], "mapping", f"unknown method/status {p['payment_method']}/{p['status']}"); continue
        if amt is None or amt < 0 or dk is None or not txn:
            reject(rej, "payments", p["id"], "type_cast", "bad amount/date/transaction id"); continue
        out.append(dict(payment_id=p["id"], order_id=p["order_id"], provider_transaction_id=txn, date_key=dk,
                        customer_user_id=o["user_id"], payment_method=method, payment_status=status, amount=amt))
    return out


def t_shipment(raw, ctx, rej):
    out, seen = [], set()
    for s in raw["shipment"]:
        if s["id"] in seen:
            reject(rej, "shipment", s["id"], "dedup", "duplicate shipment id"); continue
        seen.add(s["id"])
        o, status = ctx["orders"].get(s["order_id"]), canon(s["status"], SHIPMENT_STATUS)
        if o is None:
            reject(rej, "shipment", s["id"], "orphan", "order not found"); continue
        if status is None:
            reject(rej, "shipment", s["id"], "mapping", f"unknown status {s['status']}"); continue
        shipped, est, actual, odate = (to_date(s["shipped_date"]), to_date(s["estimated_delivery_date"]),
                                       to_date(s["actual_delivery_date"]), to_date(o["order_date"]))
        if shipped and actual and actual < shipped:
            reject(rej, "shipment", s["id"], "delivery_before_ship", "actual delivery before shipped date"); continue
        out.append(dict(
            shipment_id=s["id"], order_id=s["order_id"], tracking_number=norm_text(s["tracking_number"]),
            customer_user_id=o["user_id"], address_id=o["shipping_address_id"],
            carrier_name=smart_title(s["carrier_name"]) or "Unknown", shipment_status=status,
            shipped_date_key=date_key(shipped), estimated_delivery_date_key=date_key(est),
            actual_delivery_date_key=date_key(actual),
            days_to_ship=(shipped - odate).days if shipped and odate else None,
            days_to_deliver=(actual - shipped).days if actual and shipped else None,
            days_late=(actual - est).days if actual and est else None))
    return out


def t_return(raw, ctx, rej):
    out, seen = [], set()
    for r in raw["returns"]:
        if r["id"] in seen:
            reject(rej, "returns", r["id"], "dedup", "duplicate return id"); continue
        seen.add(r["id"])
        item = ctx["items"].get(r["order_item_id"])
        o = ctx["orders"].get(item["order_id"]) if item else None
        seller = ctx["seller"].get(item["product_id"]) if item else None
        if o is None or seller is None:
            reject(rej, "returns", r["id"], "orphan", "order item / order / product not found"); continue
        status, refund, dk = canon(r["status"], RETURN_STATUS), money(r["refund_amount"]), date_key(r["return_date"])
        if status is None or refund is None or dk is None:
            reject(rej, "returns", r["id"], "type_cast", "unknown status or bad refund/date"); continue
        if refund < 0 or refund > money(item["subtotal"]):
            reject(rej, "returns", r["id"], "refund_rule", f"refund {refund} > item subtotal {item['subtotal']}"); continue
        out.append(dict(return_id=r["id"], order_item_id=r["order_item_id"], date_key=dk,
                        return_date=to_date(r["return_date"]), customer_user_id=o["user_id"],
                        product_id=item["product_id"], seller_user_id=seller, return_status=status,
                        reason=norm_text(r["reason"]), refund_amount=refund))
    return out


def t_review(raw, rej):
    out, seen = [], set()
    for r in raw["reviews"]:
        if r["id"] in seen:
            reject(rej, "product_reviews", r["id"], "dedup", "duplicate review id"); continue
        seen.add(r["id"])
        dk = date_key(r["created_at"])
        if r["rating"] is None or not 1 <= int(r["rating"]) <= 5:
            reject(rej, "product_reviews", r["id"], "rating_rule", f"rating {r['rating']} not in 1..5"); continue
        if dk is None:
            reject(rej, "product_reviews", r["id"], "type_cast", "missing review date"); continue
        out.append(dict(review_id=r["id"], order_item_id=r["order_item_id"], date_key=dk,
                        review_date=to_date(r["created_at"]), customer_user_id=r["user_id"],
                        product_id=r["product_id"], rating=int(r["rating"])))
    return out


def t_inventory(raw, today, rej):
    out = []
    for r in raw["inventory"]:
        if r["stock_quantity"] is None or r["stock_quantity"] < 0:
            reject(rej, "products", r["product_id"], "stock_rule", "negative/NULL stock"); continue
        out.append(dict(date_key=date_key(today), snapshot_date=today, product_id=r["product_id"],
                        seller_user_id=r["seller_id"], stock_quantity=int(r["stock_quantity"])))
    return out


def t_cart_item(raw, ctx, rej):
    latest = {}
    for c in raw["cart_items"]:                     # rows come ordered by added_at -> last one wins
        latest[(c["cart_id"], c["product_id"])] = c
    out = []
    for (cart_id, pid), c in latest.items():
        user, dk = ctx["carts"].get(cart_id), date_key(c["added_at"])
        if user is None:
            reject(rej, "cart_items", f"{cart_id}-{pid}", "orphan", "cart not found"); continue
        if dk is None or c["quantity"] is None or c["quantity"] <= 0:
            reject(rej, "cart_items", f"{cart_id}-{pid}", "quantity_rule", "bad quantity/date"); continue
        out.append(dict(cart_id=cart_id, product_id=pid, date_key=dk, added_date=to_date(c["added_at"]),
                        customer_user_id=user, quantity=int(c["quantity"])))
    return out


# ----------------------------------------------------------------------------- driver
def transform_all(raw, today: date, m):
    rej, clean, ctx = [], {}, _ctx(raw)
    steps = [
        ("customer",      len(raw["users"]),         lambda: t_customer(raw, rej)),
        ("seller",        len(raw["products"]),      lambda: t_seller(raw, rej)),
        ("geography",     len(raw["addresses"]),     lambda: t_geography(raw, rej)),
        ("product",       len(raw["products"]),      lambda: t_product(raw, rej)),
        ("coupon",        len(raw["coupons"]),       lambda: t_coupon(raw, rej)),
        ("tag",           len(raw["tags"]),          lambda: t_tag(raw, rej)),
        ("product_tag",   len(raw["product_tags"]),  lambda: t_product_tag(raw, rej)),
        ("sales",         len(raw["order_items"]),   lambda: t_sales(raw, ctx, rej)),
        ("order_coupon",  len(raw["order_coupons"]), lambda: t_order_coupon(raw, ctx, rej)),
        ("payment",       len(raw["payments"]),      lambda: t_payment(raw, ctx, rej)),
        ("shipment",      len(raw["shipment"]),      lambda: t_shipment(raw, ctx, rej)),
        ("return",        len(raw["returns"]),       lambda: t_return(raw, ctx, rej)),
        ("review",        len(raw["reviews"]),       lambda: t_review(raw, rej)),
        ("inventory",     len(raw["inventory"]),     lambda: t_inventory(raw, today, rej)),
        ("cart_item",     len(raw["cart_items"]),    lambda: t_cart_item(raw, ctx, rej)),
    ]
    for name, n_in, fn in steps:
        before, t0 = len(rej), time.perf_counter()
        clean[name] = fn()
        m.record("Transform", name, n_in, len(clean[name]), len(rej) - before, time.perf_counter() - t0)
    return clean, rej
