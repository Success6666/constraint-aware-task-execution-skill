## Design

### 1. Preserve the existing parser contract

- Keep the current public parsing entry point, arguments, return type, and valid-input output unchanged.
- Do not alter token meanings, whitespace handling, ordering, defaults, or accepted syntax.
- Keep any existing exception type compatible. If no parser-specific exception exists, define `ParseError(ValueError)` in `app/parser.py`; callers catching `ValueError` will continue to work.
- The parser must either return a complete result or raise—never return a partially parsed result.

### 2. Make malformed input deterministic

Add explicit checks at every point where the parser currently assumes valid input:

- Unexpected characters or tokens.
- Missing values after separators or keys.
- Unterminated quoted strings or grouped constructs.
- Missing closing delimiters.
- Truncated input at end-of-file.
- Invalid escape sequences, if escapes are supported.
- Duplicate or conflicting declarations only if the existing grammar already treats them as invalid.

Malformed input should:

- Raise the parser error consistently.
- Include the input location when available: line, column, and a short reason.
- Never leak `IndexError`, `KeyError`, `TypeError`, or an infinite loop caused by failed token consumption.
- Leave no externally visible partial state.

Use small helper functions for token advancement, delimiter matching, and error construction so every failure follows the same path. Every loop that consumes input must either advance or raise.

### 3. Backward-compatibility strategy

- Reuse the existing tokenization and semantic conversion behavior for valid input.
- Add validation only around previously unchecked boundaries.
- Do not silently discard malformed fragments or reinterpret them as valid input.
- Keep error formatting additive: callers should be able to catch the same broad exception category even if the message now contains location details.
- Preserve behavior for empty input, comments, optional sections, and trailing whitespace according to the current implementation.

### 4. Documentation update

Document:

- The accepted input format and existing parser return behavior.
- That malformed input raises the parser error rather than producing partial output.
- The error’s location information.
- One valid example and one malformed example.
- Whether callers should catch the parser-specific error or its compatible base class.

Do not document unsupported recovery or automatic correction.

## Regression tests

Add tests covering both compatibility and failure behavior.

### Existing valid behavior

Retain or add regression cases for:

- The smallest valid input.
- Multiple entries.
- Whitespace and trailing newline handling.
- Comments, if supported.
- Quoted and escaped values, if supported.
- Optional fields and defaults.
- Existing output ordering and value types.
- Empty input, if currently accepted.

Assert the complete parsed result, not merely that parsing succeeds.

### Malformed-input matrix

Parameterize malformed examples for:

- Unexpected token or character.
- Missing required value.
- Missing separator.
- Unterminated quote.
- Unterminated grouping or missing closing delimiter.
- Invalid escape.
- Input ending immediately after a prefix, key, or separator.
- Junk after an otherwise complete construct.
- Any previously reported malformed-input regression.

For each case, assert:

- The parser raises the documented parser error.
- The error identifies the relevant line and column when location reporting is part of the API.
- No unrelated built-in exception escapes.

### Safety and compatibility assertions

- Verify malformed input does not return a partial result.
- Verify valid inputs produce exactly the same results as before the change.
- Verify the parser error remains catchable through `ValueError` if that is the compatibility base.
- Include a multiline malformed example to verify line/column tracking.
- Keep tests confined to the existing parser test suite and document the behavior in the existing README.
