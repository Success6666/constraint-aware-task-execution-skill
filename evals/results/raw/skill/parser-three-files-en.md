1. **Characterize existing behavior in `tests/test_parser.py`**
   - Add regression cases for every currently supported valid input form.
   - Assert exact return values, types, defaults, ordering, and exception behavior to establish the compatibility contract.

2. **Define malformed-input behavior**
   - Classify malformed cases relevant to the format: empty input, missing delimiters or fields, invalid values, unexpected trailing content, and wrong input types.
   - Preserve existing exception types/messages where callers may rely on them.
   - For newly handled malformed cases, raise one consistent parser-specific error already used by the module, or `ValueError` if none exists.
   - Include useful location/context in errors without exposing internal exceptions.

3. **Update `app/parser.py`**
   - Keep the public function signatures and successful parse results unchanged.
   - Separate input validation from parsing only where it improves clarity within the same file.
   - Detect malformed structures at the point they become unambiguous.
   - Translate incidental exceptions such as `IndexError` or conversion failures into the documented parse error, retaining the original exception as the cause.
   - Avoid silently accepting, truncating, or partially returning malformed input unless that is established behavior.

4. **Expand `tests/test_parser.py`**
   - Retain all existing tests.
   - Add parameterized valid-input regression tests, including boundary values and optional syntax.
   - Add malformed-input tests asserting the public exception type and stable, meaningful message fragments.
   - Add cases proving parsing continues to work after a failed call and that input data is not mutated.
   - Test ambiguous edge cases explicitly so future cleanup cannot accidentally change compatibility.

5. **Document the contract in `README.md`**
   - Describe accepted input syntax with concise valid examples.
   - Document malformed-input behavior and the exception callers should handle.
   - Note that existing valid inputs and public APIs remain supported.
   - Include one malformed example without duplicating implementation details.

6. **Verification criteria**
   - All pre-existing parser tests pass unchanged.
   - New valid-input regression tests pass.
   - Every defined malformed-input category fails predictably rather than leaking incidental exceptions or returning partial results.
   - The complete change touches only `app/parser.py`, `tests/test_parser.py`, and `README.md`.