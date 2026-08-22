Build the tool as a small Python CLI using the standard library.

1. **Command interface**

   ```powershell
   python sales_summary.py sales.csv --output summary.csv
   ```

   Support:

   - One or more input CSV files
   - Required output path
   - Optional date format, defaulting to ISO `YYYY-MM-DD`
   - Optional encoding, defaulting to UTF-8

2. **Expected input**

   Require these columns:

   ```text
   date,product,quantity,unit_price
   ```

   Example:

   ```csv
   date,product,quantity,unit_price
   2026-01-05,Widget A,2,12.50
   2026-01-18,Widget A,3,12.50
   2026-02-02,Widget B,1,20.00
   ```

3. **Read and validate**

   Use `csv.DictReader` to stream rows without loading entire files into memory.

   For each row:

   - Parse `date` with `datetime.strptime`
   - Normalize the month to `YYYY-MM`
   - Trim the product name
   - Parse `quantity` as `Decimal`
   - Parse `unit_price` as `Decimal`
   - Calculate row revenue as `quantity * unit_price`
   - Report malformed rows with filename and row number

   Use `Decimal` rather than `float` to avoid currency rounding errors.

4. **Aggregate**

   Store totals in a dictionary keyed by `(product, month)`:

   ```python
   totals[(product, month)] = {
       "quantity": Decimal("0"),
       "revenue": Decimal("0"),
       "transactions": 0,
   }
   ```

   Update the matching entry for every valid row.

5. **Export**

   Use `csv.DictWriter` to produce:

   ```text
   product,month,total_quantity,total_revenue,transaction_count
   ```

   Sort output by product and then month. Round monetary totals to two decimal places using an explicit decimal rounding mode.

6. **Error behavior**

   - Exit with a nonzero status when files cannot be read or required columns are absent.
   - Treat malformed data rows according to a CLI option such as `--on-error fail|skip`, defaulting to `fail`.
   - Write diagnostics to standard error.
   - Avoid leaving a partial output file by writing only after processing succeeds.

7. **Suggested structure**

   ```text
   sales_summary.py
   tests/
     test_sales_summary.py
     fixtures/
       sales.csv
   README.md
   ```

   Keep the implementation separated into focused functions:

   ```python
   parse_args()
   read_sales_rows(paths, date_format, encoding)
   aggregate_sales(rows)
   write_summary(output_path, totals, encoding)
   main()
   ```

8. **Tests**

   Cover:

   - Multiple rows for the same product and month
   - The same product across different months
   - Multiple input files
   - Decimal currency calculations
   - Missing required columns
   - Invalid dates and numeric values
   - Empty input files
   - Deterministic output ordering
   - `fail` and `skip` error modes
   - Product names containing commas or quotes

Use Python’s `argparse`, `csv`, `datetime`, `decimal`, and `pathlib` modules; no third-party dependencies are necessary.