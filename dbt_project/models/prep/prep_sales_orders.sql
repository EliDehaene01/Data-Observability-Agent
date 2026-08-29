-- Joins landing_vbak + landing_vbap to the order-line grain. This is the
-- layer where source-to-target divergence is deliberately introduced --
-- row counts here differ from the source vbak/vbap tables on purpose.

with headers as (

    select *
    from {{ ref('landing_vbak') }}
    -- Business rule 1: cancelled orders are excluded entirely. They must
    -- never appear in this model or in anything built on top of it.
    where status != 'cancelled'

),

items as (

    select *
    from {{ ref('landing_vbap') }}

)

select
    h.order_id,
    h.customer_id,
    h.order_date,
    h.status,
    -- Business rule 2: incomplete orders are kept (not excluded), but
    -- flagged so downstream consumers can decide whether to include them.
    h.status = 'incomplete' as is_incomplete,
    i.item_id,
    i.material_id,
    i.quantity,
    i.net_value
from headers h
inner join items i on h.order_id = i.order_id
