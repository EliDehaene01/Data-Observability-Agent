-- 1:1 with source vbak. Cleaned/typed columns only -- no filtering, no
-- business logic belongs in this model.

select
    cast(order_id as integer)   as order_id,
    cast(customer_id as varchar) as customer_id,
    cast(order_date as date)    as order_date,
    cast(status as varchar)     as status
from {{ source('mock_erp', 'vbak') }}
