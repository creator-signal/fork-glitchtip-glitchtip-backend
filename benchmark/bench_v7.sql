INSERT INTO events_v7 (id, event_id, created, project_id, data)
VALUES (
    generate_uuid_v7(), 
    uuid_generate_v4(), 
    CURRENT_TIMESTAMP,
    (random() * 100)::int,
    '{"key": "value", "stack": "trace"}'::jsonb
);
