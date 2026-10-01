USE amazon_dw;

SET SESSION cte_max_recursion_depth = 5000;

INSERT IGNORE INTO dim_date
  (date_key, full_date, day_of_month, month_num, month_name,
   quarter, year, weekday_name, is_weekend)
WITH RECURSIVE cal AS (
  SELECT DATE '2020-01-01' AS dt
  UNION ALL
  SELECT dt + INTERVAL 1 DAY FROM cal WHERE dt < '2030-12-31'
)
SELECT
  CAST(DATE_FORMAT(dt, '%Y%m%d') AS UNSIGNED) AS date_key,
  dt                                          AS full_date,
  DAY(dt)                                     AS day_of_month,
  MONTH(dt)                                   AS month_num,
  MONTHNAME(dt)                               AS month_name,
  QUARTER(dt)                                 AS quarter,
  YEAR(dt)                                    AS year,
  DAYNAME(dt)                                 AS weekday_name,
  DAYOFWEEK(dt) IN (1, 7)                     AS is_weekend   -- 1 = Sunday, 7 = Saturday
FROM cal;
