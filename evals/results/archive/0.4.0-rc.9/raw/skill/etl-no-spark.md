Use a bounded hourly batch implemented in Python with PyArrow for Parquet, DuckDB for disk-backed transformations, and a small transactional metadata database for checkpoints and deduplication.

**Event Contract**

Require an envelope such as:

```json
{
  "event_id": "01J...",
  "event_type": "order.created",
  "event_time": "2026-08-16T10:42:31.123Z",
  "producer": "checkout",
  "schema_version": 2,
  "payload": {}
}
```

Define schemas by `(event_type, schema_version)`. Validate:

- Required fields and types
- UTC timestamp parsing
- Known event type and schema version
- Event-specific payload
- Maximum record size
- Optional business constraints

Invalid JSON and schema failures go to quarantine JSONL with the source file, line number, error code, and original record.

**Hourly Flow**

```text
Discover input snapshot
        |
Parse and validate JSONL
        |
Write valid rows into DuckDB staging
        |
Deterministically deduplicate
        |
Reserve unseen event IDs transactionally
        |
Write immutable Parquet files
        |
Publish commit manifest
        |
Mark checkpoint complete
```

1. Snapshot all input objects belonging to the closed hour. Record URI, size, modification time, and preferably an object-store version or checksum.
2. Derive a stable `run_id` from the input hour and sorted input fingerprints.
3. Stream JSONL rather than loading the hour into memory. Convert valid records into Arrow batches and append them to a DuckDB staging table.
4. Deduplicate within the batch using `event_id`. Choose the winner deterministically by source URI and line number.
5. Compare candidates with a durable `seen_events` table. Reserve new IDs for this `run_id` in one transaction.
6. Write only reserved events as compressed Parquet, sorted by `event_time` and `event_id`.
7. Publish a small commit manifest listing every output file, row count, checksum, schema version, and partition.
8. Mark the run complete. Consumers read only files referenced by committed manifests.

For the same `event_id` with different content, keep the deterministic winner but quarantine the collision and emit a high-severity metric. Silently discarding conflicting events hides producer defects.

**Output Layout**

Partition by event time, not processing time:

```text
events/
  event_type=order.created/
    event_date=2026-08-16/
      event_hour=10/
        run_id=<id>/
          part-00000.parquet

commits/
  input_date=2026-08-16/
    input_hour=11/
      <run_id>.json

quarantine/
  input_date=2026-08-16/
    input_hour=11/
      <run_id>.jsonl
```

Use Zstandard compression and target files around 128–512 MB. Avoid producing one file per source object.

Late events naturally land in their event-time partition. Retain `ingested_at`, `source_uri`, and `source_line` as lineage columns.

**Checkpoint State**

A transactional metadata database can use these tables:

```sql
CREATE TABLE runs (
    run_id          VARCHAR PRIMARY KEY,
    input_hour      TIMESTAMP NOT NULL,
    input_snapshot  JSON NOT NULL,
    status          VARCHAR NOT NULL,
    manifest_uri    VARCHAR,
    valid_count     BIGINT DEFAULT 0,
    invalid_count   BIGINT DEFAULT 0,
    duplicate_count BIGINT DEFAULT 0,
    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL
);

CREATE TABLE seen_events (
    event_id     VARCHAR PRIMARY KEY,
    payload_hash VARCHAR NOT NULL,
    run_id       VARCHAR NOT NULL,
    event_time   TIMESTAMP NOT NULL
);
```

Recommended states:

```text
DISCOVERED -> VALIDATED -> IDS_RESERVED -> FILES_WRITTEN -> COMMITTED
```

Recovery resumes from the last state rather than starting a different run:

- Before `IDS_RESERVED`: repeat validation safely.
- After reservation: the same `run_id` owns those IDs.
- After files are written: verify checksums and publish the manifest.
- After manifest publication: marking the checkpoint complete is repeatable.
- An existing valid commit manifest means the run succeeded, even if the final metadata update was interrupted.

On object storage, do not depend on directory rename being atomic. Immutable files plus an atomically created commit manifest provide the publication boundary.

DuckDB is suitable for a single pipeline writer. If multiple workers may process hours concurrently, put `runs` and `seen_events` in PostgreSQL or another database supporting unique constraints and transactions.

**Core Dedup Query**

```sql
WITH ranked AS (
    SELECT *,
           row_number() OVER (
               PARTITION BY event_id
               ORDER BY source_uri, source_line
           ) AS duplicate_rank,
           count(*) OVER (
               PARTITION BY event_id
           ) AS duplicate_count
    FROM staged_events
)
SELECT *
FROM ranked
WHERE duplicate_rank = 1;
```

Store a canonical payload hash so duplicates and conflicting reuse of an ID can be distinguished.

**Test Strategy**

Unit tests:

- Valid and invalid JSON
- Each schema version
- Missing, extra, and incorrectly typed fields
- Timestamp normalization
- Canonical payload hashing
- Deterministic duplicate selection
- Partition derivation around UTC hour/day boundaries

Integration tests:

- Multiple input files with duplicates across files
- Duplicate IDs from earlier hours
- Conflicting payloads for one ID
- Late events written to older event-time partitions
- Expected Parquet schema, compression, partitions, and row counts
- Quarantine contents and reason codes
- Empty input hour

Recovery tests should inject failure after every state transition, restart the pipeline, and assert:

- Exactly one committed manifest
- No duplicate committed event IDs
- No loss of reserved events
- Stable output checksums on retries
- Correct checkpoint and metric totals

Finally, run property-based tests generating randomized JSON records and duplicates, then assert that committed IDs are unique and every input record is accounted for as committed, duplicate, or quarantined.