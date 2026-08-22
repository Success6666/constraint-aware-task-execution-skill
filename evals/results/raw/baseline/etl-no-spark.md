# Hourly JSONL ETL Design

## 1. Technology

Use Python with:

- `orjson` for JSONL parsing
- `pydantic` or `fastjsonschema` for schema validation
- `pyarrow` for Parquet writing and schema enforcement
- `duckdb` or SQLite for checkpoint and dedup metadata
- Object storage or a POSIX filesystem for input, output, and quarantine data

No Spark is required.

## 2. Event Contract

Each input line must contain:

```json
{
  "event_id": "evt_123",
  "event_type": "purchase",
  "event_time": "2025-01-15T10:23:14.123Z",
  "ingest_time": "2025-01-15T10:23:20.456Z",
  "source": "checkout",
  "schema_version": 1,
  "payload": {
    "customer_id": "cus_42",
    "amount": 19.95,
    "currency": "USD"
  }
}
```

Required fields:

- `event_id`: globally unique, non-empty string
- `event_type`: controlled string
- `event_time`: UTC timestamp
- `ingest_time`: UTC timestamp
- `source`: source identifier
- `schema_version`: integer
- `payload`: object

Validation rules:

- Reject malformed JSON.
- Reject missing or incorrectly typed fields.
- Reject timestamps that cannot be parsed as UTC.
- Reject unsupported schema versions.
- Enforce event-specific payload schemas.
- Preserve the original line and validation error in quarantine output.

A canonical payload hash should be computed from normalized JSON:

```text
payload_hash = SHA256(canonical_json(event))
```

Conflicting records with the same `event_id` but different hashes must be quarantined for investigation.

## 3. Processing Window

Each run processes one logical UTC hour:

```text
window_start = 2025-01-15T10:00:00Z
window_end   = 2025-01-15T11:00:00Z
```

Input selection should use:

```text
window_start <= event_time < window_end
```

The scheduler should start a window only after a configurable lateness delay, for example six hours. This provides a watermark:

```text
watermark = current_utc_time - 6 hours
```

Events arriving after the watermark are handled by a late-data correction run.

## 4. Pipeline Stages

### Stage A: Discover Input

The runner identifies all input JSONL objects covering the processing window and records:

- object identifier
- content length
- checksum or version identifier
- discovery timestamp

The complete input set becomes part of the checkpoint. A retry must use the same input snapshot unless an explicit correction run is requested.

### Stage B: Parse and Validate

Read JSONL incrementally in batches to bound memory usage.

For every record, produce one of:

- valid event
- malformed JSON quarantine record
- schema validation quarantine record
- unsupported-version quarantine record

Validation should happen before any deduplication.

### Stage C: Normalize

Normalize valid records:

- Convert timestamps to UTC.
- Normalize field names and types.
- Canonicalize decimal amounts.
- Serialize the complete normalized event deterministically.
- Calculate `payload_hash`.
- Add processing metadata:
  - `processing_window_start`
  - `processing_run_id`
  - `schema_version`

### Stage D: Deduplicate

Deduplication key:

```text
event_id
```

Within the current run:

1. Group by `event_id`.
2. If all hashes match, retain one record.
3. If hashes differ, quarantine all conflicting records.
4. Select the deterministic winner using:

```text
lowest ingest_time,
then lowest source,
then lowest payload_hash
```

Across runs and hourly windows, consult a durable deduplication index containing:

```text
event_id
payload_hash
event_time
first_seen_at
output_partition
status
```

Rules:

- Existing `event_id` with the same hash: skip as a duplicate.
- Existing `event_id` with a different hash: quarantine as a conflict.
- New `event_id`: insert into the dedup index as part of the commit transaction.

The dedup index must be retained for the maximum possible replay and late-arrival period, or indefinitely if event IDs are globally reusable.

## 5. Output Dataset

Write Parquet using a stable physical schema:

```text
event_id                 string
event_type               string
event_time               timestamp[us, UTC]
ingest_time              timestamp[us, UTC]
source                   string
schema_version           int32
payload                  struct or normalized columns
payload_json             string
payload_hash             string
processing_window_start  timestamp[us, UTC]
processing_run_id        string
```

Partition by event time:

```text
event_date=YYYY-MM-DD/event_hour=HH
```

This supports hourly reads and avoids partitioning by high-cardinality fields such as `event_id`.

Recommended file properties:

- Snappy compression
- Row groups sized around 64-256 MB
- Stable column ordering
- One or more Parquet objects per partition
- Explicit Arrow schema, never inferred independently per batch

For highly variable payloads, use both:

- typed columns for frequently queried fields
- `payload_json` for the complete normalized payload

## 6. Exactly-Once Output Commit

Use immutable run staging and an atomic commit protocol.

For each run:

1. Create a unique `run_id`.
2. Write validated output to a staging location.
3. Write quarantine records separately.
4. Validate row counts, schema, and checksums.
5. Write a manifest containing:
   - run ID
   - logical window
   - input snapshot
   - output object identifiers
   - row counts
   - duplicate counts
   - quarantine counts
   - schema version
   - content checksums
6. Commit the checkpoint only after output and manifest publication succeeds.

A retry first checks the checkpoint:

- `COMMITTED`: return success without writing again.
- `RUNNING`: resume or mark stale and retry safely.
- `FAILED`: retry using the same input snapshot.
- absent: create a new run.

Output publication must be idempotent. The manifest is the authoritative list of active output objects. Consumers should read committed manifests rather than partially written staging data.

## 7. Late Data and Corrections

Late events should be processed by a correction run associated with the original event-time hour.

Two acceptable approaches:

### Preferred: Versioned Partition Replacement

For an affected partition:

1. Read the currently committed partition.
2. Merge late events.
3. Deduplicate against the durable index.
4. Write a new partition version.
5. Publish a new manifest superseding the previous version.
6. Mark the old version inactive.

Readers use the latest committed partition version.

### Alternative: Append-Only Delta Objects

Write late events as additional Parquet objects and require readers to deduplicate by `event_id`. This is simpler operationally but moves complexity to every consumer.

Use versioned replacement when downstream users expect a clean, unique dataset.

## 8. Checkpoint State

Checkpoint records should include:

```text
window_start
window_end
run_id
status
input_snapshot
watermark
started_at
completed_at
output_manifest_id
valid_row_count
duplicate_row_count
conflict_count
quarantine_row_count
error_message
```

State transitions:

```text
NEW -> RUNNING -> VALIDATED -> COMMITTED
                         -> FAILED
```

Only `COMMITTED` is terminal success.

Checkpoint updates must be transactional. A run cannot become `COMMITTED` unless:

- all expected input objects were processed
- Parquet schema validation passed
- output checksums were recorded
- the output manifest was published
- deduplication state was persisted

## 9. Recovery Behavior

The runner must tolerate:

- process termination during parsing
- termination during Parquet writing
- duplicate scheduler invocations
- partial staging output
- checkpoint database failure
- input object becoming unavailable
- malformed records
- late events
- conflicting duplicate IDs

Recovery procedure:

1. Acquire a lease for the logical window.
2. Inspect checkpoint state.
3. Reuse the recorded input snapshot.
4. Ignore or clean uncommitted staging data using the run ID.
5. Resume from the last completed batch if batch-level progress is recorded.
6. Rebuild staged output if progress metadata is unavailable.
7. Publish output and checkpoint atomically.
8. Release the lease.

Batch-level progress can include:

```text
run_id
input_object_id
byte_offset
batch_number
batch_checksum
status
```

The pipeline must not rely on process memory for recovery.

## 10. Metrics and Alerts

Emit metrics for:

- input objects and bytes
- parsed records
- valid records
- malformed records
- schema failures
- unsupported versions
- duplicates
- duplicate conflicts
- late events
- output rows and bytes
- processing duration
- checkpoint age
- retry count
- watermark lag

Alert on:

- missing expected input
- repeated checkpoint failures
- conflict rate above threshold
- quarantine rate above threshold
- schema drift
- output row count unexpectedly zero
- processing time approaching the hourly schedule interval
- stalled windows

## 11. Verification Strategy

### Unit Tests

Test:

- valid event acceptance
- missing and invalid fields
- malformed JSON
- unsupported schema versions
- timestamp normalization
- canonical hashing
- same-ID identical duplicates
- same-ID conflicting duplicates
- deterministic winner selection
- partition derivation
- late-event classification
- schema-to-Arrow conversion

### Integration Tests

Run against a temporary object store or local filesystem and checkpoint database.

Verify:

- one hourly input produces the expected Parquet partition
- Parquet schema is stable across batches
- rerunning a committed hour produces no duplicate output
- a failed run can resume
- process termination before commit leaves no visible partial output
- duplicate scheduler executions serialize correctly
- quarantine output contains original records and reasons
- late events replace or correctly extend the target partition
- conflicting IDs never enter the clean dataset

### Property Tests

Generate arbitrary valid and invalid records and verify:

- deduplication is idempotent
- processing the same input twice yields the same logical dataset
- output row uniqueness holds for `event_id`
- partition assignment is deterministic
- record ordering does not change the selected winner

### End-to-End Acceptance Criteria

The design is acceptable when:

1. Every committed output row conforms to the declared schema.
2. No clean output contains two records with the same `event_id`.
3. Conflicting duplicate IDs are quarantined.
4. A retry after any pre-commit failure produces the same result as a first successful run.
5. A committed run is never published twice.
6. Late data is visible in the correct event-time partition.
7. Consumers can discover complete output exclusively through committed manifests.
8. All invalid input remains traceable through quarantine records and run metadata.
