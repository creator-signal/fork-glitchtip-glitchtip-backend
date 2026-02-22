-- SpanStaging: Write-heavy staging table for span data
-- Partitioned by DAY on `created` (datetime range, no HASH sub-partitioning)
-- No indexes — optimized for bulk inserts, read only by promotion job
-- Rows are promoted to Parquet and deleted within hours

CREATE TABLE IF NOT EXISTS performance_spanstaging (
    -- 8-byte alignment
    id BIGSERIAL,
    organization_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    duration DOUBLE PRECISION NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Variable-width fields
    transaction_name VARCHAR(1024) NOT NULL,
    span_id VARCHAR(32) NOT NULL,
    transaction_id VARCHAR(32) NOT NULL,
    op VARCHAR(255) NOT NULL,
    description VARCHAR(500) NOT NULL DEFAULT '',

    PRIMARY KEY (id, created)
) PARTITION BY RANGE (created);
