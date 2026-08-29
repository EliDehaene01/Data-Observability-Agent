-- Business rule 1 (see prep_sales_orders.sql) must hold all the way through
-- to serve_sales_orders. Query must return zero rows for the test to pass.

select *
from {{ ref('serve_sales_orders') }}
where order_status = 'cancelled'
