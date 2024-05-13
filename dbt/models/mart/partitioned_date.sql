{{
    config(
        materialized='table',
        partition_by={
            'field': 'date',
            'data_type': 'timestamp',
            'granularity': 'day'
        }
    )
}}

  SELECT
    *
  FROM
    {{ ref('fact_table') }}