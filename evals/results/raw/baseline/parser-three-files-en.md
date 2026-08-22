**Design**

Extend the existing parser while preserving its current public entry point, return type, and output for every valid input. The change should be limited to:

- Parser behavior in `app/parser.py`
- Regression coverage in `tests/test_parser.py`
- User-facing behavior documentation in `README.md`

**Malformed-input policy**

Classify malformed input into three categories:

1. **Lexical errors**
   - Invalid characters
   - Unterminated strings
   - Invalid escape sequences
   - Invalid numeric or token syntax

2. **Structural errors**
   - Missing delimiters
   - Unexpected delimiters
   - Truncated input
   - Missing required fields or sections

3. **Semantic errors**
   - Correctly formed syntax with unsupported values
   - Duplicate fields where uniqueness is required
   - Invalid combinations of otherwise valid fields

All malformed input should produce one deterministic parser error through the parser’s existing error mechanism. Do not allow raw `IndexError`, `KeyError`, `TypeError`, or similar implementation exceptions to escape.

The error should include:

- A stable, human-readable category or message
- The relevant input position when available
- Enough context to identify the malformed construct
- The original exception as the cause when wrapping an existing low-level parsing error

Catch only expected parsing and validation failures. Do not use a broad catch-all that hides programming defects.

**Backward-compatibility rules**

- Valid input must produce byte-for-byte or value-for-value identical results to the current implementation.
- Existing accepted whitespace, quoting, ordering, and optional-field behavior must remain unchanged.
- Existing callers must not need to change their invocation.
- Existing parser error types should remain usable. If a dedicated parser exception already exists, reuse it. If not, introduce a parser-specific exception without changing the normal return contract.
- Do not silently skip malformed input or partially return data unless the current parser already documents that behavior.
- Error messages should be stable enough for tests to assert on their category and location, but tests should avoid depending on incidental wording.

**Implementation shape**

1. Keep the current parsing stages and successful code paths intact.
2. Add explicit validation at the boundaries where malformed input currently causes incidental exceptions:
   - Before indexing into token or character collections
   - Before consuming required delimiters
   - Before converting token text to typed values
   - Before accessing required fields
3. Centralize conversion of low-level failures into the parser’s public error type.
4. Include the parser position in errors using the parser’s existing offset, line, or column convention. If no convention exists, use a zero-based character offset consistently.
5. Ensure end-of-input is handled explicitly so truncated input reports “unexpected end of input” rather than an index error.
6. Preserve partial internal state only as an implementation detail; never expose incomplete results after a parse failure.
7. Keep the parser deterministic: the same malformed input should always produce the same error category and location.

**Regression tests**

Add tests covering both compatibility and failure behavior.

Valid-input compatibility tests:

- Existing representative minimal input
- Input exercising optional fields
- Input containing whitespace and formatting variations already supported
- Input containing multiple records or nested structures, if applicable
- Inputs using every supported scalar type or token form
- Assert exact parsed output, not merely successful completion

Malformed-input tests:

- Empty input where input is required
- Truncated input at each required delimiter
- Unexpected closing delimiter
- Invalid token or character
- Unterminated quoted value
- Invalid escape sequence
- Invalid numeric or typed value
- Missing required field
- Duplicate field, if duplicates are invalid
- Unsupported semantic value
- Malformed input ending immediately after a valid prefix

For each malformed case, assert:

- The parser raises or returns the documented parser error according to the existing API
- The error is the parser-specific error type
- The error identifies the correct category or stable message fragment
- The error position is correct where position tracking is supported
- No raw implementation exception escapes
- No partial result is returned

Also add a regression test proving that malformed input does not affect later parses when the same parser instance or shared parser state is reused.

**Documentation**

Update the README parser documentation to state:

- What input forms are accepted
- That valid-input behavior is unchanged
- How malformed input is reported
- The parser error type or return convention
- Whether position information is included
- A short malformed-input example
- Whether callers should treat parser errors as input-validation failures

Avoid documenting incidental exception text. Document the stable error contract instead.

**Verification**

Run the complete parser test suite and the broader test suite. Verify that:

- All pre-existing tests pass unchanged.
- New malformed-input tests pass.
- Valid outputs are unchanged.
- No raw indexing, conversion, or lookup exceptions escape.
- Error locations remain correct for multiline and truncated input.
- The README matches the actual parser behavior.
