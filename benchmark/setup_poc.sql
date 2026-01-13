CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- BASELINE: Partitioned by created, PK (created, id), Random UUIDv4
CREATE TABLE events_baseline (
    created timestamptz NOT NULL,
    id uuid NOT NULL DEFAULT uuid_generate_v4(),
    project_id int NOT NULL,
    data jsonb,
    PRIMARY KEY (created, id)
) PARTITION BY RANGE (created);

-- Range extended to 2027 to cover current date (Jan 2026)
CREATE TABLE events_baseline_p1 PARTITION OF events_baseline 
    FOR VALUES FROM ('2025-01-01') TO ('2027-01-01');

-- CHALLENGER: Partitioned by ID (UUIDv7), PK (id), Dual ID (v7 + v4)
CREATE TABLE events_v7 (
    id uuid NOT NULL, 
    event_id uuid NOT NULL, 
    created timestamptz NOT NULL,
    project_id int NOT NULL,
    data jsonb,
    PRIMARY KEY (id)
) PARTITION BY RANGE (id);

CREATE TABLE events_v7_p1 PARTITION OF events_v7 
    FOR VALUES FROM (MINVALUE) TO (MAXVALUE);

CREATE INDEX events_v7_client_id_idx ON events_v7 (event_id);

-- Polyfill for UUIDv7 generation in Postgres < 17
CREATE OR REPLACE FUNCTION generate_uuid_v7() RETURNS uuid AS $$
DECLARE
    v_time timestamp with time zone := clock_timestamp();
    v_secs bigint := EXTRACT(EPOCH FROM v_time);
    v_msec bigint := mod(cast(EXTRACT(MILLISECONDS FROM v_time) as bigint), 1000);
    v_usec bigint := mod(cast(EXTRACT(MICROSECONDS FROM v_time) as bigint), 1000);
    v_timestamp bigint := (((v_secs * 1000) + v_msec) << 12) | (v_usec & 4095);
    v_random bigint := (random() * 4611686018427387903::bigint)::bigint;
    v_uuid_hex text;
BEGIN
    v_uuid_hex := lpad(to_hex(v_timestamp), 12, '0') || '7' || 
                  lpad(to_hex((v_random & 4323455642275676159::bigint) | 4611686018427387904::bigint), 3, '0') ||
                  lpad(to_hex((random() * 9223372036854775807::bigint)::bigint), 16, '0');
    RETURN v_uuid_hex::uuid;
END;
$$ LANGUAGE plpgsql;
