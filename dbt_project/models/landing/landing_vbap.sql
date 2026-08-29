-- 1:1 with source vbap. Cleaned/typed columns only -- no filtering, no
-- business logic belongs in this model. order_id is a FK to landing_vbak
-- (enforced by the relationships test in schema.yml, not joined here).

select
    cast(order_id as integer)      as order_id,
    cast(item_id as integer)       as item_id,
    cast(material_id as varchar)   as material_id,
    cast(quantity as integer)      as quantity,
    cast(net_value as decimal(15, 2)) as net_value
from {{ source('mock_erp', 'vbap') }}
