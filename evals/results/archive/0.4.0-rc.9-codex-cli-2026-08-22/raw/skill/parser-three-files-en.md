Plan:

1. **Clarify the existing parser contract**
   - Document current accepted syntax, return shape, and exception behavior.
   - Treat all currently valid inputs as compatibility cases whose output must remain unchanged.

2. **Update `app/parser.py`**
   - Add explicit validation around tokenization and structural parsing.
   - Detect malformed input such as truncated records, invalid delimiters, missing required fields, and unexpected trailing data.
   - Use the parser’s existing failure convention where possible; if none exists, introduce a small, documented parser-specific exception without changing successful-call behavior.
   - Ensure errors identify the failure and, where practical, the input position.
   - Keep parsing logic deterministic and avoid silently accepting corrupted structures.

3. **Add regression coverage in `tests/test_parser.py`**
   - Preserve representative valid-input tests, including boundary and legacy formats.
   - Add malformed-input tests for each failure category.
   - Assert the documented exception/type and useful error details.
   - Add compatibility tests confirming valid inputs produce exactly the prior results.
   - Include empty input and truncated input cases.

4. **Document behavior in `README.md`**
   - Describe the accepted format and malformed-input policy.
   - Show a short valid-input example and how callers should handle parse failures.
   - Call out that existing valid inputs remain backward-compatible.

5. **Verification**
   - Run the parser test suite and the full test suite.
   - Review the diff to confirm only `app/parser.py`, `tests/test_parser.py`, and `README.md` are changed.