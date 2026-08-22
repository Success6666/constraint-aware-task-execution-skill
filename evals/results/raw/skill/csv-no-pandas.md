# CLI Sales Summary Design

## Objective

Build a Python command-line program that:

1. Reads one or more CSV inputs.
2. Groups sales by `product` and calendar month.
3. Calculates quantity and revenue totals.
4. Writes a deterministic summary CSV.
5. Uses only the Python standard library; do not use pandas.

## Input CSV schema

Each input must contain these columns:

```text
product,date,quantity,unit_price
```

Example:

```csv
product,date,quantity,unit_price
Widget A,2025-01-15,2,19.99
Widget A,2025-01-20,3,19.99
Widget B,2025-02-03,1,49.50
```

Rules:

- `product`: required non-empty text.
- `date`: ISO date or datetime, such as `2025-01-15` or `2025-01-15T14:30:00`.
- `quantity`: non-negative decimal number.
- `unit_price`: non-negative decimal number.
- Revenue is calculated as `quantity × unit_price`.
- Currency values use `Decimal`, not binary floating point.

## Command-line interface

Example command:

```text
sales-summary input1.csv input2.csv --output summary.csv
```

Arguments:

```text
sales-summary INPUT [INPUT ...] --output OUTPUT
```

Optional arguments:

```text
--delimiter DELIMITER     Input delimiter, default ","
--encoding ENCODING       Input encoding, default "utf-8-sig"
--date-column NAME        Default "date"
--product-column NAME     Default "product"
--quantity-column NAME    Default "quantity"
--price-column NAME       Default "unit_price"
```

Behavior:

- Multiple inputs are processed as one dataset.
- Every input must have the required columns.
- Invalid rows produce a clear error containing the input name and row number.
- Exit with a nonzero status if parsing or writing fails.
- Do not silently skip invalid data.

## Summary CSV schema

The output columns are:

```text
product,month,total_quantity,total_revenue,sale_count
```

Example:

```csv
product,month,total_quantity,total_revenue,sale_count
Widget A,2025-01,5,99.95,2
Widget B,2025-02,1,49.50,1
```

Definitions:

- `product`: grouping product.
- `month`: `YYYY-MM`, derived from the sale date.
- `total_quantity`: sum of quantities in the group.
- `total_revenue`: sum of calculated row revenue.
- `sale_count`: number of input rows in the group.

Output ordering:

1. Product ascending lexicographically.
2. Month ascending chronologically.

## Recommended implementation structure

Use a small standard-library Python package:

```text
sales_summary/
    __main__.py
    cli.py
    parser.py
    aggregation.py
    exporter.py
```

Responsibilities:

- `cli.py`
  - Define arguments with `argparse`.
  - Coordinate reading, aggregation, and export.
  - Convert expected errors into concise stderr messages and exit code `1`.

- `parser.py`
  - Read CSV rows using `csv.DictReader`.
  - Validate required headers.
  - Parse dates with `datetime`.
  - Parse numeric values with `Decimal`.
  - Yield normalized records.

- `aggregation.py`
  - Use a dictionary keyed by `(product, month)`.
  - Maintain `total_quantity`, `total_revenue`, and `sale_count`.

- `exporter.py`
  - Sort aggregate records.
  - Write using `csv.DictWriter`.
  - Format decimal values consistently.

## Aggregation algorithm

For each row:

```text
month = parsed_date.strftime("%Y-%m")
revenue = quantity * unit_price
key = (product, month)

if key does not exist:
    initialize totals to zero

total_quantity += quantity
total_revenue += revenue
sale_count += 1
```

Use a `dataclass` for normalized rows and aggregate values where useful.

Quantization policy:

- Preserve exact decimal arithmetic during aggregation.
- Quantize monetary output to two decimal places using `Decimal("0.01")`.
- Format quantities without unnecessary trailing zeroes.
- Reject negative quantity or price unless a future business rule explicitly adds returns/refunds.

## Validation rules

Reject:

- Missing required headers.
- Empty product values.
- Invalid dates.
- Invalid numeric values.
- Negative quantities or prices.
- Rows with missing required values.
- Output paths that cannot be written.

The parser should report errors in a form such as:

```text
input1.csv: row 7: invalid quantity 'abc'
```

## Verification plan

### Unit tests

Test:

1. One row produces one summary group.
2. Multiple rows with the same product and month are combined.
3. Rows for the same product in different months remain separate.
4. Rows for different products remain separate.
5. Revenue uses decimal arithmetic correctly.
6. Datetime input is grouped by its calendar month.
7. Empty products are rejected.
8. Invalid dates and numbers are rejected.
9. Negative values are rejected.
10. Missing headers are rejected.
11. Output ordering is deterministic.
12. Custom column names and delimiters work.

### Integration tests

Run the CLI against representative inputs and verify:

- Expected output headers.
- Correct totals.
- Correct `sale_count`.
- Correct ordering.
- Multiple input sources aggregate together.
- Invalid input returns a nonzero exit status and useful error text.

### Example verification case

Input:

```csv
product,date,quantity,unit_price
A,2025-01-01,2,10.00
A,2025-01-31,3,10.00
A,2025-02-01,1,10.00
B,2025-01-05,4,2.50
```

Expected output:

```csv
product,month,total_quantity,total_revenue,sale_count
A,2025-01,5,50.00,2
A,2025-02,1,10.00,1
B,2025-01,4,10.00,1
```

This design is stream-oriented, deterministic, dependency-free, and directly implementable with Python’s `argparse`, `csv`, `datetime`, and `decimal` modules.
