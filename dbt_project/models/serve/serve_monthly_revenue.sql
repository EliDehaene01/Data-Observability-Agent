-- Aggregates prep_sales_orders by customer and month. Business-friendly
-- names only -- no new business logic (that belongs in prep/).

select
    customer_id,
    date_trunc('month', order_date) as revenue_month,
    sum(net_value)                  as total_net_value,
    count(distinct order_id)        as order_count,
    count(*)                        as item_count
from {{ ref('prep_sales_orders') }}
group by 1, 2
