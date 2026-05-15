-- IssueSearchIndex: Hash-partitioned by organization_id
-- Holds full-text search vectors decoupled from the Issue table.
-- Hash partitions are created by RunPython (uses settings.PARTITION_HASH_BUCKETS).

CREATE TABLE IF NOT EXISTS issue_events_issuesearchindex (
    -- 8-byte alignment: FKs
    issue_id BIGINT NOT NULL,
    organization_id INTEGER NOT NULL,

    -- Variable-width: tsvector
    fts_document TSVECTOR NOT NULL DEFAULT '',

    PRIMARY KEY (issue_id, organization_id)
) PARTITION BY HASH (organization_id);

-- GIN index for full-text search (created on parent, inherited by partitions)
CREATE INDEX issuesearchindex_fts_gin
    ON issue_events_issuesearchindex USING GIN (fts_document);

-- No DB-level FK constraints: partitioned tables cannot be the target of a
-- foreign key, and DB-level FKs on the partitioned side cause "cannot truncate
-- a table referenced in a foreign key constraint" during Django's test flush.
-- ORM FK fields (DO_NOTHING) still provide joins and validation.
