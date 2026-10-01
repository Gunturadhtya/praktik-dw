-- =====================================================================
-- Amazon Clone Data Warehouse (Kimball star schema), MySQL 8
-- Converted from dim.dbml
-- Notes:
--   * Natural keys have UNIQUE indexes so ETL loads are idempotent.
--   * dim_product is SCD Type 2: unique on (product_id, effective_from).
--   * Mandatory fact FKs are NOT NULL (orphan test target = 0).
--     Optional role-playing dates in fact_shipment are NULLable.
-- =====================================================================

CREATE DATABASE IF NOT EXISTS amazon_dw
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE amazon_dw;

-- ======================= DIMENSIONS ==================================

CREATE TABLE IF NOT EXISTS dim_date (
  date_key      INT NOT NULL COMMENT 'yyyymmdd, e.g. 20260930',
  full_date     DATE NOT NULL,
  day_of_month  TINYINT,
  month_num     TINYINT,
  month_name    VARCHAR(20),
  quarter       TINYINT,
  year          SMALLINT,
  weekday_name  VARCHAR(20),
  is_weekend    BOOLEAN,
  PRIMARY KEY (date_key),
  UNIQUE KEY uq_dim_date_full_date (full_date)
);

CREATE TABLE IF NOT EXISTS dim_customer (
  customer_key  BIGINT NOT NULL AUTO_INCREMENT,
  user_id       BIGINT NOT NULL COMMENT 'business key from users.id',
  email         VARCHAR(255),
  full_name     VARCHAR(255),
  registered_at TIMESTAMP NULL,
  PRIMARY KEY (customer_key),
  UNIQUE KEY uq_dim_customer_user_id (user_id)
);

CREATE TABLE IF NOT EXISTS dim_seller (
  seller_key BIGINT NOT NULL AUTO_INCREMENT,
  user_id    BIGINT NOT NULL COMMENT 'business key from users.id (seller role)',
  email      VARCHAR(255),
  full_name  VARCHAR(255),
  PRIMARY KEY (seller_key),
  UNIQUE KEY uq_dim_seller_user_id (user_id)
);

CREATE TABLE IF NOT EXISTS dim_product (
  product_key     BIGINT NOT NULL AUTO_INCREMENT,
  product_id      BIGINT NOT NULL COMMENT 'business key from products.id',
  name            VARCHAR(255),
  description     TEXT,
  category_level1 VARCHAR(255),
  category_level2 VARCHAR(255),
  category_level3 VARCHAR(255),
  current_price   DECIMAL(12,2),
  effective_from  DATE NOT NULL COMMENT 'SCD Type 2',
  effective_to    DATE NOT NULL DEFAULT '9999-12-31' COMMENT 'SCD Type 2, 9999-12-31 if current',
  is_current      BOOLEAN NOT NULL DEFAULT 1,
  PRIMARY KEY (product_key),
  UNIQUE KEY uq_dim_product_version (product_id, effective_from),
  KEY idx_dim_product_current (product_id, is_current)
);

CREATE TABLE IF NOT EXISTS dim_geography (
  geo_key     BIGINT NOT NULL AUTO_INCREMENT,
  address_id  BIGINT NOT NULL COMMENT 'business key from addresses.id',
  city        VARCHAR(255),
  state       VARCHAR(255),
  postal_code VARCHAR(255),
  country     VARCHAR(255),
  PRIMARY KEY (geo_key),
  UNIQUE KEY uq_dim_geography_address_id (address_id)
);

CREATE TABLE IF NOT EXISTS dim_order_status (
  order_status_key INT NOT NULL AUTO_INCREMENT,
  status_name      VARCHAR(20) NOT NULL COMMENT 'Pending, Processed, Shipped, Delivered, Cancelled',
  PRIMARY KEY (order_status_key),
  UNIQUE KEY uq_dim_order_status_name (status_name)
);

CREATE TABLE IF NOT EXISTS dim_payment_method (
  payment_method_key INT NOT NULL AUTO_INCREMENT,
  method_name        VARCHAR(20) NOT NULL COMMENT 'Credit Card, Wallet, Transfer',
  PRIMARY KEY (payment_method_key),
  UNIQUE KEY uq_dim_payment_method_name (method_name)
);

CREATE TABLE IF NOT EXISTS dim_payment_status (
  payment_status_key INT NOT NULL AUTO_INCREMENT,
  status_name        VARCHAR(20) NOT NULL COMMENT 'Success, Failed, Pending',
  PRIMARY KEY (payment_status_key),
  UNIQUE KEY uq_dim_payment_status_name (status_name)
);

CREATE TABLE IF NOT EXISTS dim_shipment_status (
  shipment_status_key INT NOT NULL AUTO_INCREMENT,
  status_name         VARCHAR(20) NOT NULL COMMENT 'Preparing, In_Transit, Delivered, Returned',
  PRIMARY KEY (shipment_status_key),
  UNIQUE KEY uq_dim_shipment_status_name (status_name)
);

CREATE TABLE IF NOT EXISTS dim_return_status (
  return_status_key INT NOT NULL AUTO_INCREMENT,
  status_name       VARCHAR(20) NOT NULL COMMENT 'Requested, Approved, Rejected, Refunded',
  PRIMARY KEY (return_status_key),
  UNIQUE KEY uq_dim_return_status_name (status_name)
);

CREATE TABLE IF NOT EXISTS dim_carrier (
  carrier_key  INT NOT NULL AUTO_INCREMENT,
  carrier_name VARCHAR(255) NOT NULL,
  PRIMARY KEY (carrier_key),
  UNIQUE KEY uq_dim_carrier_name (carrier_name)
);

CREATE TABLE IF NOT EXISTS dim_coupon (
  coupon_key     INT NOT NULL AUTO_INCREMENT,
  coupon_id      INT NOT NULL COMMENT 'business key from coupons.id',
  code           VARCHAR(255),
  discount_type  VARCHAR(20) COMMENT 'Percentage, Fixed',
  discount_value DECIMAL(12,2),
  valid_from     DATETIME,
  valid_until    DATETIME,
  PRIMARY KEY (coupon_key),
  UNIQUE KEY uq_dim_coupon_coupon_id (coupon_id)
);

CREATE TABLE IF NOT EXISTS dim_tag (
  tag_key INT NOT NULL AUTO_INCREMENT,
  tag_id  INT NOT NULL COMMENT 'business key from tags.id',
  name    VARCHAR(255),
  PRIMARY KEY (tag_key),
  UNIQUE KEY uq_dim_tag_tag_id (tag_id)
);

CREATE TABLE IF NOT EXISTS bridge_product_tag (
  product_key BIGINT NOT NULL,
  tag_key     INT NOT NULL,
  PRIMARY KEY (product_key, tag_key),
  CONSTRAINT fk_bpt_product FOREIGN KEY (product_key) REFERENCES dim_product (product_key),
  CONSTRAINT fk_bpt_tag     FOREIGN KEY (tag_key)     REFERENCES dim_tag (tag_key)
);

-- ========================= FACTS =====================================

-- Grain: one order line (order_items)
CREATE TABLE IF NOT EXISTS fact_sales (
  order_item_id      BIGINT NOT NULL COMMENT 'degenerate dimension',
  order_id           BIGINT NOT NULL COMMENT 'degenerate dimension',
  date_key           INT NOT NULL,
  customer_key       BIGINT NOT NULL,
  seller_key         BIGINT NOT NULL,
  product_key        BIGINT NOT NULL,
  ship_to_geo_key    BIGINT NOT NULL,
  order_status_key   INT NOT NULL,
  quantity           INT NOT NULL,
  unit_price         DECIMAL(12,2) NOT NULL,
  subtotal           DECIMAL(12,2) NOT NULL,
  allocated_discount DECIMAL(12,2) NOT NULL DEFAULT 0 COMMENT 'order-level discount split across lines',
  PRIMARY KEY (order_item_id),
  KEY idx_fs_order (order_id),
  CONSTRAINT fk_fs_date     FOREIGN KEY (date_key)         REFERENCES dim_date (date_key),
  CONSTRAINT fk_fs_customer FOREIGN KEY (customer_key)     REFERENCES dim_customer (customer_key),
  CONSTRAINT fk_fs_seller   FOREIGN KEY (seller_key)       REFERENCES dim_seller (seller_key),
  CONSTRAINT fk_fs_product  FOREIGN KEY (product_key)      REFERENCES dim_product (product_key),
  CONSTRAINT fk_fs_geo      FOREIGN KEY (ship_to_geo_key)  REFERENCES dim_geography (geo_key),
  CONSTRAINT fk_fs_status   FOREIGN KEY (order_status_key) REFERENCES dim_order_status (order_status_key)
);

-- Grain: one coupon applied to one order
CREATE TABLE IF NOT EXISTS fact_order_coupon (
  order_id         BIGINT NOT NULL COMMENT 'degenerate dimension',
  coupon_key       INT NOT NULL,
  date_key         INT NOT NULL,
  customer_key     BIGINT NOT NULL,
  discount_applied DECIMAL(12,2) NOT NULL,
  PRIMARY KEY (order_id, coupon_key),
  CONSTRAINT fk_foc_date     FOREIGN KEY (date_key)     REFERENCES dim_date (date_key),
  CONSTRAINT fk_foc_customer FOREIGN KEY (customer_key) REFERENCES dim_customer (customer_key),
  CONSTRAINT fk_foc_coupon   FOREIGN KEY (coupon_key)   REFERENCES dim_coupon (coupon_key)
);

-- Grain: one payment attempt
CREATE TABLE IF NOT EXISTS fact_payment (
  payment_id              BIGINT NOT NULL COMMENT 'degenerate dimension',
  order_id                BIGINT NOT NULL COMMENT 'degenerate dimension',
  provider_transaction_id VARCHAR(255) NOT NULL COMMENT 'degenerate dimension',
  date_key                INT NOT NULL,
  customer_key            BIGINT NOT NULL,
  payment_method_key      INT NOT NULL,
  payment_status_key      INT NOT NULL,
  amount                  DECIMAL(12,2) NOT NULL,
  PRIMARY KEY (payment_id),
  UNIQUE KEY uq_fp_provider_txn (provider_transaction_id),
  KEY idx_fp_order (order_id),
  CONSTRAINT fk_fp_date     FOREIGN KEY (date_key)           REFERENCES dim_date (date_key),
  CONSTRAINT fk_fp_customer FOREIGN KEY (customer_key)       REFERENCES dim_customer (customer_key),
  CONSTRAINT fk_fp_method   FOREIGN KEY (payment_method_key) REFERENCES dim_payment_method (payment_method_key),
  CONSTRAINT fk_fp_status   FOREIGN KEY (payment_status_key) REFERENCES dim_payment_status (payment_status_key)
);

-- Grain: one shipment (accumulating snapshot, row updated per milestone)
CREATE TABLE IF NOT EXISTS fact_shipment (
  shipment_id                 BIGINT NOT NULL COMMENT 'degenerate dimension',
  order_id                    BIGINT NOT NULL COMMENT 'degenerate dimension',
  tracking_number             VARCHAR(255) NOT NULL COMMENT 'degenerate dimension',
  customer_key                BIGINT NOT NULL,
  ship_to_geo_key             BIGINT NOT NULL,
  carrier_key                 INT NOT NULL,
  shipment_status_key         INT NOT NULL,
  shipped_date_key            INT NULL COMMENT 'role-playing date',
  estimated_delivery_date_key INT NULL COMMENT 'role-playing date',
  actual_delivery_date_key    INT NULL COMMENT 'role-playing date',
  days_to_ship                INT NULL COMMENT 'lag measure',
  days_to_deliver             INT NULL COMMENT 'lag measure',
  days_late                   INT NULL COMMENT 'actual vs estimated',
  PRIMARY KEY (shipment_id),
  KEY idx_fsh_order (order_id),
  CONSTRAINT fk_fsh_customer FOREIGN KEY (customer_key)                REFERENCES dim_customer (customer_key),
  CONSTRAINT fk_fsh_geo      FOREIGN KEY (ship_to_geo_key)             REFERENCES dim_geography (geo_key),
  CONSTRAINT fk_fsh_carrier  FOREIGN KEY (carrier_key)                 REFERENCES dim_carrier (carrier_key),
  CONSTRAINT fk_fsh_status   FOREIGN KEY (shipment_status_key)         REFERENCES dim_shipment_status (shipment_status_key),
  CONSTRAINT fk_fsh_shipped  FOREIGN KEY (shipped_date_key)            REFERENCES dim_date (date_key),
  CONSTRAINT fk_fsh_est      FOREIGN KEY (estimated_delivery_date_key) REFERENCES dim_date (date_key),
  CONSTRAINT fk_fsh_actual   FOREIGN KEY (actual_delivery_date_key)    REFERENCES dim_date (date_key)
);

-- Grain: one return request
CREATE TABLE IF NOT EXISTS fact_return (
  return_id         INT NOT NULL COMMENT 'degenerate dimension',
  order_item_id     BIGINT NOT NULL COMMENT 'degenerate dimension',
  date_key          INT NOT NULL,
  customer_key      BIGINT NOT NULL,
  product_key       BIGINT NOT NULL,
  seller_key        BIGINT NOT NULL,
  return_status_key INT NOT NULL,
  reason            TEXT,
  refund_amount     DECIMAL(12,2) NOT NULL,
  PRIMARY KEY (return_id),
  KEY idx_fr_order_item (order_item_id),
  CONSTRAINT fk_fr_date     FOREIGN KEY (date_key)          REFERENCES dim_date (date_key),
  CONSTRAINT fk_fr_customer FOREIGN KEY (customer_key)      REFERENCES dim_customer (customer_key),
  CONSTRAINT fk_fr_product  FOREIGN KEY (product_key)       REFERENCES dim_product (product_key),
  CONSTRAINT fk_fr_seller   FOREIGN KEY (seller_key)        REFERENCES dim_seller (seller_key),
  CONSTRAINT fk_fr_status   FOREIGN KEY (return_status_key) REFERENCES dim_return_status (return_status_key)
);

-- Grain: one review
CREATE TABLE IF NOT EXISTS fact_review (
  review_id     INT NOT NULL COMMENT 'degenerate dimension',
  order_item_id BIGINT NOT NULL COMMENT 'degenerate dimension',
  date_key      INT NOT NULL,
  customer_key  BIGINT NOT NULL,
  product_key   BIGINT NOT NULL,
  rating        TINYINT NOT NULL,
  PRIMARY KEY (review_id),
  UNIQUE KEY uq_frv_order_item (order_item_id),
  CONSTRAINT fk_frv_date     FOREIGN KEY (date_key)     REFERENCES dim_date (date_key),
  CONSTRAINT fk_frv_customer FOREIGN KEY (customer_key) REFERENCES dim_customer (customer_key),
  CONSTRAINT fk_frv_product  FOREIGN KEY (product_key)  REFERENCES dim_product (product_key)
);

-- Grain: one product per day (periodic snapshot)
CREATE TABLE IF NOT EXISTS fact_inventory_daily (
  date_key       INT NOT NULL,
  product_key    BIGINT NOT NULL,
  seller_key     BIGINT NOT NULL,
  stock_quantity INT NOT NULL COMMENT 'semi-additive: do not sum across dates',
  PRIMARY KEY (date_key, product_key),
  CONSTRAINT fk_fi_date    FOREIGN KEY (date_key)    REFERENCES dim_date (date_key),
  CONSTRAINT fk_fi_product FOREIGN KEY (product_key) REFERENCES dim_product (product_key),
  CONSTRAINT fk_fi_seller  FOREIGN KEY (seller_key)  REFERENCES dim_seller (seller_key)
);

-- Grain: one product in one cart (optional, for funnel analysis)
CREATE TABLE IF NOT EXISTS fact_cart_item (
  cart_id      BIGINT NOT NULL COMMENT 'degenerate dimension',
  product_key  BIGINT NOT NULL,
  date_key     INT NOT NULL COMMENT 'from added_at',
  customer_key BIGINT NOT NULL,
  quantity     INT NOT NULL,
  PRIMARY KEY (cart_id, product_key),
  CONSTRAINT fk_fci_date     FOREIGN KEY (date_key)     REFERENCES dim_date (date_key),
  CONSTRAINT fk_fci_customer FOREIGN KEY (customer_key) REFERENCES dim_customer (customer_key),
  CONSTRAINT fk_fci_product  FOREIGN KEY (product_key)  REFERENCES dim_product (product_key)
);
