-- landing_vbap's primary key is composite (order_id, item_id). dbt's built-in
-- `unique` test only covers single columns, so this checks the composite key
-- directly: the query must return zero rows for the test to pass.

select order_id, item_id, count(*) as row_count
from {{ ref('landing_vbap') }}
group by order_id, item_id
having count(*) > 1
