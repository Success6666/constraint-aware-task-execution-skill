Use a Python pipeline built around `PyArrow`, a compiled JSON Schema validator such as `fastjsonschema`, and a transactional state store. SQLite is sufficient for one worker; PostgreSQL is the better choice when multiple workers or hosts are involved.

**Data Flow**

```text
Hourly JSONL inputs
      |
      v
Streaming reader -> JSON parse -> Schema validation
      |                                  |
      |                                  +-> Quarantine JSONL
      v
Normalize -> Deduplicate -> Buffered Arrow batches
                              |
                              v
                 Staged partitioned Parquet
                              |
                              v
             Transactional checkpoint + manifest
                              |
                              v
                    Published partitions
```

Process input incrementally rather than loading an entire hour into memory.

### Input contract

Each event should contain at least:

```json
{
  "event_id": "uuid-or-stable-id",
  "event_time": "2026-08-16T12:34:56.123Z",
  "event_type": "purchase",
  "payload": {}
}
```

Require a stable `event_id`. If producers cannot provide one, derive it from canonicalized business fields with SHA-256, but document that this changes deduplication semantics.

Inputs should be immutable and discoverable by an hourly prefix such as:

```text
incoming/2026/08/16/12/*.jsonl
```

Record each input object's path, size, checksum or version ID. This prevents silently processing a file that changed after discovery.

### Validation

Maintain a versioned JSON Schema for the input envelope. The processing sequence is:

1. Parse each line independently.
2. Validate required fields, types, formats, and supported schema version.
3. Normalize timestamps to UTC and enforce an explicit PyArrow output schema.
4. Send invalid records to quarantine without aborting the hour, unless an error threshold is exceeded.

Quarantine records should include:

```json
{
  "source": "incoming/.../events-01.jsonl",
  "line_number": 42,
  "error_code": "SCHEMA_VALIDATION_FAILED",
  "error": "event_time is required",
  "raw_line": "...",
  "run_id": "..."
}
```

Configure absolute and percentage-based failure thresholds so a corrupt input does not produce a misleading “successful” partition.

### Deduplication

Use two layers:

- In-batch deduplication: a hash set of `event_id` values removes repeats within the current batch.
- Persistent deduplication: a transactional table with a unique key on `(event_id, dedupe_scope)` handles duplicates across files, retries, and adjacent hours.

Recommended state table:

```sql
CREATE TABLE processed_events (
    dedupe_scope TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_time TEXT NOT NULL,
    run_id TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (dedupe_scope, event_id)
);
```

Choose the scope explicitly:

- Global, for IDs guaranteed unique forever.
- Event type plus ID, if IDs are only unique within a type.
- Time-windowed, if storage must be bounded.

For windowed deduplication, retain IDs longer than the maximum accepted lateness, for example 14 days for a 7-day late-arrival policy. Do not delete deduplication state merely because its source hour completed.

### Parquet layout

Partition by event time, not ingestion time:

```text
output/event_date=2026-08-16/event_hour=12/
    part-<run-id>-00000.parquet
```

Optionally add a low-cardinality partition such as `event_type`; avoid high-cardinality fields like customer or event ID.

Use:

- Snappy or Zstandard compression.
- Explicit Arrow types.
- Target files around 128–512 MB.
- UTC timestamp columns.
- Deterministic column ordering.
- Statistics enabled for predicate pushdown.

Late events are written to their event-time partition as additional immutable Parquet files. Periodic compaction can merge small files without changing logical contents.

### Checkpoint and publication protocol

A checkpoint should track source identity and precise progress:

```text
runs:
  run_id
  scheduled_hour
  status: RUNNING | READY_TO_PUBLISH | PUBLISHED | FAILED
  schema_version
  input_snapshot
  started_at
  updated_at

source_checkpoints:
  run_id
  source_path
  source_checksum
  byte_offset
  line_number
  status

output_files:
  run_id
  final_path
  checksum
  row_count
  min_event_time
  max_event_time
  status
```

Use an immutable-file plus manifest protocol:

1. Discover and freeze the input snapshot.
2. Resume from the last committed source offset.
3. Write Parquet files to a run-specific staging location.
4. Close files and verify checksums and row counts.
5. In one database transaction:
   - Insert deduplication keys.
   - Save source checkpoints.
   - Register output files.
   - Mark the run `READY_TO_PUBLISH`.
6. Publish an hourly or run manifest with atomic rename or conditional create.
7. Mark the run `PUBLISHED`.

Consumers read only files referenced by published manifests. A crash before step 5 leaves disposable staging files. A crash after step 5 resumes publication instead of reprocessing records. A crash after step 6 detects the existing manifest and completes the final state transition.

Only one writer should own a scheduled hour at a time. Enforce this with a database lease or unique constraint, including lease expiry for dead-worker recovery.

### Operational behavior

Expose metrics for:

- Input, valid, invalid, output, and duplicate record counts.
- Validation errors by code.
- Processing lag and duration.
- Parquet file count and size.
- Checkpoint age.
- Recovery attempts.
- Late-event count.

The run should fail when input identity changes, publication conflicts, state cannot be committed, or validation thresholds are exceeded. Individual malformed lines can be quarantined.

### Suggested package structure

```text
etl/
  cli.py
  config.py
  discovery.py
  reader.py
  validation.py
  normalization.py
  dedup.py
  parquet_writer.py
  checkpoint.py
  publisher.py
  pipeline.py
schemas/
  event-v1.json
tests/
  unit/
  integration/
  recovery/
```

A CLI entry point could be:

```text
python -m etl.cli run-hour --hour 2026-08-16T12:00:00Z
```

### Test strategy

Unit tests should cover:

- Valid and invalid JSON.
- Every schema constraint.
- Timestamp normalization and partition selection.
- Deterministic fallback ID generation.
- Duplicate detection within and across batches.
- Deduplication retention boundaries.
- Parquet schema and null handling.

Integration tests should process small fixture files and verify:

- Exact output rows and partitions.
- Invalid-record quarantine.
- Multiple inputs for one hour.
- Late events written to earlier partitions.
- Re-running an hour produces no additional logical records.
- Parquet files are readable by PyArrow and DuckDB.

Recovery tests should inject failures after each durability boundary:

- During input reading.
- During Parquet writing.
- After file close but before checkpoint commit.
- After checkpoint commit but before manifest publication.
- After manifest publication but before marking the run published.

For every failure point, restart the process and assert that no event is lost, no event is published twice, and no uncommitted file becomes visible.

Property-based tests with Hypothesis are useful for random JSON values, duplicate arrangements, batch boundaries, and crash points. A final end-to-end invariant should assert:

```text
published valid unique events
= valid input events
- duplicates under the configured dedupe scope
```

This design provides effectively-once published output without Spark, while keeping memory usage bounded and recovery behavior deterministic.