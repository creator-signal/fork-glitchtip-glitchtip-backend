INSERT INTO events_baseline (created, id, project_id, data)
VALUES (
    CURRENT_TIMESTAMP, 
    uuid_generate_v4(), 
    (random() * 100)::int, 
    '{"key": "value", "stack": "trace"}'::jsonb
);
