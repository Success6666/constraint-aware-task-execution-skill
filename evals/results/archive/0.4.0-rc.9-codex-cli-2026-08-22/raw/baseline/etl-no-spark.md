## Architecture

Use a small Python service or containerized batch job, triggered at the start of every hour.

Suggested components:

- Python 3.12
- `pydantic` for strict schema validation
- `orjson` for fast JSONL parsing
- `pyarrow` for Parquet writing and partitioned datasets
- DuckDB or SQLite for checkpoints, manifests, and the durable deduplication index
- Cron, Airflow, Dagster, or a queue-based scheduler for hourly execution

```text
raw JSONL
   |
   v
discover hour -> validate -> normalize -> deduplicate
                                      |
                                      v
                           write Parquet staging files
                                      |
                                      v
                         atomic commit + checkpoint update
                                      |
                                      v
                         partitioned Parquet dataset
```

## Input Layout

Use immutable, hour-scoped input paths:

```text
raw/
  ingest_date=2026-08-22/
    ingest_hour=13/
      source-a-001.jsonl
      source-a-002.jsonl
```

Each input file should have a stable identity, such as:

```text
(source_path, file_size, modification_time, sha256)
```

The file hash prevents accidental reprocessing of changed input under the same name.

## Event Schema

Example canonical event:

```python
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: str = Field(min_length=1, max_length=256)
    event_ts: datetime
    event_type: str = Field(min_length=1, max_length=128)
    schema_version: int = Field(ge=1)
    payload: dict[str, Any]
    source: str = Field(min_length=1, max_length=128)
```

Normalize before writing:

- Convert timestamps to UTC.
- Normalize field names and types.
- Add `ingest_ts`.
- Derive `event_date` and `event_hour`.
- Store the original source path and input file hash for traceability.

Invalid records go to a quarantine dataset:

```text
quarantine/
  ingest_date=2026-08-22/
    ingest_hour=13/
      invalid.jsonl
```

Each quarantined row should include the raw line, validation error, source path, and line number.

## Deduplication

Use `event_id` as the primary deduplication key. If producers cannot guarantee globally unique IDs, use a deterministic fingerprint:

```python
fingerprint = sha256(
    canonical_json({
        "source": event["source"],
        "event_ts": event["event_ts"],
        "event_type": event["event_type"],
        "payload": event["payload"],
    })
).hexdigest()
```

Maintain a durable index in DuckDB or SQLite:

```sql
CREATE TABLE processed_events (
    event_id VARCHAR PRIMARY KEY,
    event_ts TIMESTAMP NOT NULL,
    output_file VARCHAR NOT NULL,
    committed_at TIMESTAMP NOT NULL
);
```

Deduplication happens at two levels:

1. In-memory set for duplicates within the current hour.
2. Database lookup for IDs already committed by earlier runs.

For large volumes, process IDs in batches and use a temporary table:

```sql
CREATE TEMP TABLE incoming_ids(event_id VARCHAR PRIMARY KEY);

INSERT INTO incoming_ids VALUES (...);

SELECT e.*
FROM normalized_events e
LEFT JOIN processed_events p USING (event_id)
WHERE p.event_id IS NULL;
```

## Partitioned Parquet Output

Partition by fields commonly used for filtering:

```text
events/
  event_date=2026-08-22/
    event_hour=13/
      part-00001.parquet
      part-00002.parquet
```

Optionally add `event_type` if cardinality is small and queries frequently filter by it.

Use PyArrow:

```python
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

table = pa.Table.from_pylist(records)

ds.write_dataset(
    table,
    base_dir="events",
    format="parquet",
    partitioning=["event_date", "event_hour"],
    basename_template="part-{i}.parquet",
    existing_data_behavior="overwrite_or_ignore",
)
```

Avoid writing directly to the final directory. Write to a run-specific staging directory first:

```text
_events_staging/
  run_id=20260822T130000Z/
    event_date=2026-08-22/
      event_hour=13/
        part-00000.parquet
```

After successful validation, rename staging files atomically into the final dataset.

## Checkpoint and Recovery

Store one row per hourly run:

```sql
CREATE TABLE checkpoints (
    ingest_date DATE NOT NULL,
    ingest_hour INTEGER NOT NULL,
    run_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL, -- RUNNING, COMMITTED, FAILED
    input_manifest_json VARCHAR NOT NULL,
    output_manifest_json VARCHAR,
    row_count BIGINT,
    started_at TIMESTAMP NOT NULL,
    committed_at TIMESTAMP,
    PRIMARY KEY (ingest_date, ingest_hour)
);
```

The input manifest contains source paths and hashes. A run is skipped only when:

- The checkpoint status is `COMMITTED`.
- The current input manifest matches the stored manifest.
- All output files listed in the output manifest still exist.

### Commit protocol

1. Acquire an hourly lock.
2. Build and persist the input manifest.
3. Mark checkpoint `RUNNING`.
4. Read JSONL and validate records.
5. Deduplicate against the durable index.
6. Write Parquet to staging.
7. Validate staged Parquet by reopening it and checking schema and row count.
8. Atomically rename staged files to their final paths.
9. In one database transaction:
   - Insert IDs into `processed_events`.
   - Store the output manifest.
   - Mark the checkpoint `COMMITTED`.
10. Release the lock.

### Crash recovery

On startup:

- Find `RUNNING` checkpoints.
- Verify staged and final files against their manifests.
- If files are complete, finish the metadata transaction.
- If files are incomplete, remove only that run’s staging directory and retry.
- Never mark a run complete based solely on the presence of a directory.

Use deterministic output names based on `run_id` or file hash so retries do not create ambiguous files.

## Processing Skeleton

```python
def process_hour(ingest_date, ingest_hour):
    lock_hour(ingest_date, ingest_hour)

    manifest = discover_inputs(ingest_date, ingest_hour)
    checkpoint = load_checkpoint(ingest_date, ingest_hour)

    if checkpoint and checkpoint.is_committed(manifest):
        return checkpoint.output_manifest

    run_id = new_run_id()
    save_checkpoint_running(ingest_date, ingest_hour, run_id, manifest)

    valid = []
    invalid = []

    for source in manifest.sources:
        for line_number, raw_line in enumerate(read_jsonl(source.path), 1):
            try:
                event = Event.model_validate_json(raw_line)
                valid.append(normalize(event, source, line_number))
            except Exception as exc:
                invalid.append(quarantine_row(source, line_number, raw_line, exc))

    write_quarantine(invalid)

    deduped = remove_intra_batch_duplicates(valid)
    deduped = remove_committed_ids(deduped)

    staged = write_staged_parquet(deduped, run_id)
    validate_parquet(staged)

    output_manifest = atomic_commit_files(staged)

    with db.transaction():
        insert_processed_event_ids(deduped, output_manifest)
        mark_checkpoint_committed(
            ingest_date,
            ingest_hour,
            run_id,
            output_manifest,
            len(deduped),
        )

    return output_manifest
```

## Operational Checks

Emit metrics for:

- Files discovered and processed
- Input lines
- Valid records
- Invalid records
- Duplicate records
- Output rows
- Processing duration
- Input and output byte counts
- Recovery attempts
- Checkpoint age

Alert on:

- Missing hourly input
- Validation rate below a threshold
- Duplicate rate spikes
- Failed or stale `RUNNING` checkpoints
- Output row count mismatch
- Corrupt or missing Parquet files

## Tests

### Unit tests

- Valid event parses successfully.
- Missing required fields fail validation.
- Unknown fields fail validation.
- Invalid timestamp and schema version fail validation.
- Timestamp normalization always produces UTC.
- Deterministic fingerprint is stable.
- Duplicate IDs within a batch are removed.
- IDs already in `processed_events` are removed.
- Partition columns are derived correctly.
- Invalid rows contain source and line metadata.

### Integration tests

Use a temporary directory and temporary DuckDB database:

1. Process one hourly input.
2. Verify Parquet schema and partition paths.
3. Run the same hour again and verify no additional rows.
4. Add a duplicate event in a later hour and verify it is ignored.
5. Add a malformed line and verify quarantine output.
6. Simulate a crash after Parquet rename but before checkpoint commit.
7. Restart and verify recovery produces one committed result.
8. Simulate incomplete staging output and verify retry behavior.
9. Change the input manifest and verify the hour is reprocessed or rejected according to policy.

### Property tests

With Hypothesis, verify that:

- Processing is idempotent.
- Reordering input lines does not change the final event set.
- Repeated retries never increase the committed count for the same `event_id`.
- Partition assignment depends only on event data, not input order.

This design avoids Apache Spark while providing strict validation, durable deduplication, partitioned Parquet storage, restartable hourly execution, and testable recovery behavior.