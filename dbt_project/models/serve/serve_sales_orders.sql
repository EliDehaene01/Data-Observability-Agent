-- Faithful rename/pass-through of prep_sales_orders -- no new logic or
-- filtering, business-friendly column names only.

select
    order_id    as sales_order_id,
    customer_id,
    order_date,
    status      as order_status,
    is_incomplete,
    item_id     as line_item_number,
    material_id as product_id,
    quantity,
    net_value
from {{ ref('prep_sales_orders') }}
