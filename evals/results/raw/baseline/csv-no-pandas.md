**Design**

Build a Python 3 command-line application using only the standard library:

- `argparse` for CLI parsing
- `csv` for reading and writing CSV
- `decimal.Decimal` for exact monetary arithmetic
- `datetime` for date parsing and month normalization
- `collections.defaultdict` for grouping
- `pathlib` for path handling

**Input Contract**

Require these CSV columns:

```text
date,product,quantity,unit_price
```

Example:

```csv
date,product,quantity,unit_price
2025-01-15,Widget A,3,19.99
2025-01-20,Widget A,2,19.99
2025-02-01,Widget B,5,7.50
```

Rules:

- `date` must use `YYYY-MM-DD`.
- `product` must be non-empty after trimming whitespace.
- `quantity` must be a positive integer.
- `unit_price` must be a non-negative decimal.
- Blank rows may be skipped.
- Headers are matched case-sensitively unless an explicit normalization policy is preferred.
- Malformed rows should produce row-numbered errors and a nonzero exit status.
- Processing should fail rather than silently produce partial results.

**Aggregation**

For each row:

```text
line_total = quantity * unit_price
month = date formatted as YYYY-MM
```

Group by:

```text
(product, month)
```

Accumulate:

```text
total_quantity += quantity
total_sales += line_total
```

Use `Decimal` throughout aggregation. Quantize exported monetary values to two decimal places:

```text
Decimal("0.01"), rounding=ROUND_HALF_UP
```

Do not convert monetary values through `float`.

**Command-Line Interface**

Provide a command named `sales-summary`:

```text
sales-summary INPUT [--output OUTPUT] [--delimiter DELIMITER] [--encoding ENCODING]
```

Recommended options:

```text
sales-summary sales.csv --output summary.csv
sales-summary sales.csv --delimiter ";" --encoding utf-8
```

Behavior:

- Read from `INPUT`.
- Write to `--output`; default to standard output.
- Write diagnostics and validation errors to standard error.
- Return exit code `0` on success.
- Return exit code `2` for invalid arguments or input data.
- Return exit code `1` for unexpected processing or output failures.
- Use `newline=""` when opening CSV streams.
- Use UTF-8 by default.

**Output Contract**

Write:

```text
month,product,total_quantity,total_sales
```

Example:

```csv
month,product,total_quantity,total_sales
2025-01,Widget A,5,99.95
2025-02,Widget B,5,37.50
```

Sorting should be deterministic:

1. `month` ascending
2. `product` ascending

The output should contain one row per product-month combination, even when multiple input rows contributed to it.

**Suggested Internal Structure**

Use small, independently testable functions:

```text
parse_args(argv)
open_input(path, encoding)
read_sales_rows(stream, delimiter)
parse_row(row, row_number)
aggregate(rows)
sort_summary(summary)
write_summary(rows, stream)
main(argv)
```

Represent validated input with a lightweight immutable structure such as:

```text
Sale:
    date: date
    product: str
    quantity: int
    unit_price: Decimal
```

Represent grouped results with:

```text
Summary:
    month: str
    product: str
    total_quantity: int
    total_sales: Decimal
```

The aggregation key should be:

```text
(month, product)
```

Using an ordered output phase rather than relying on dictionary insertion order makes sorting behavior explicit and stable.

**Validation Errors**

Include the input row number and field name where possible:

```text
row 7: quantity must be a positive integer
row 11: date must use YYYY-MM-DD
row 14: missing required column 'unit_price'
```

Handle these cases explicitly:

- Missing input
- Unreadable input
- Empty input
- Missing headers
- Missing required columns
- Extra columns
- Blank product names
- Invalid dates
- Invalid quantities
- Invalid prices
- Invalid CSV syntax
- Output write failures

Extra columns may be ignored, but missing required columns must be fatal. Document this behavior.

**Verification**

Test the following cases:

1. One row produces one summary row.
2. Multiple rows with the same product and month combine correctly.
3. Same product across different months remains separate.
4. Different products in the same month remain separate.
5. Decimal multiplication avoids floating-point errors.
6. Results are sorted by month, then product.
7. Quantity and sales totals are correct.
8. Whitespace around product values is handled consistently.
9. Invalid dates, quantities, and prices fail with row-specific errors.
10. Missing headers fail before processing rows.
11. Empty input is rejected clearly.
12. Custom delimiters work.
13. Standard-output mode produces valid CSV.
14. Output can be read back by the standard `csv` module.
15. Exit codes distinguish invalid input from unexpected failures.

A representative verification input:

```csv
date,product,quantity,unit_price
2025-02-03,Widget B,2,10.25
2025-01-15,Widget A,3,19.99
2025-01-20,Widget A,2,19.99
2025-02-10,Widget B,1,10.25
```

Expected output:

```csv
month,product,total_quantity,total_sales
2025-01,Widget A,5,99.95
2025-02,Widget B,3,30.75
```

For large inputs, process rows incrementally rather than loading all input rows into memory. Memory usage should scale with the number of distinct `(month, product)` groups, not the number of sales rows.
