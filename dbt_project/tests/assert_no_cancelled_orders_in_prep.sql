-- Business rule 1 in prep_sales_orders.sql: cancelled orders must never
-- appear here. Query must return zero rows for the test to pass.

select *
from {{ ref('prep_sales_orders') }}
where status = 'cancelled'
