1. **Define compatibility in `app/parser.py`**
   - Preserve all existing accepted inputs, return types, defaults, ordering, and exception behavior.
   - Add the new syntax through the narrowest extension point in the current parsing flow.
   - Detect malformed input explicitly before indexing, converting, or destructuring values.
   - Reject invalid input deterministically with the parser’s existing error type and message style.
   - Avoid partial results and avoid silently interpreting malformed new syntax as legacy syntax.

2. **Add regression coverage in `tests/test_parser.py`**
   - Lock in representative legacy inputs and exact outputs.
   - Cover valid forms of the new input, including boundary values and optional elements.
   - Add malformed cases for missing fields, extra separators, invalid values, truncated input, and trailing data.
   - Assert the precise failure contract: exception type and, where already stable, error message.
   - Include ambiguous inputs to prove legacy parsing still takes precedence where required.
   - Use parameterized tests where the existing test style supports them.

3. **Document the contract in `README.md`**
   - Describe the newly accepted format with valid examples.
   - List malformed forms that are rejected.
   - State compatibility behavior for existing input.
   - Document the failure behavior users can rely on without exposing implementation details.

4. **Verification criteria**
   - All pre-existing tests pass unchanged.
   - New valid-input and malformed-input tests pass.
   - No behavior changes outside the documented parser extension.
   - The final diff contains changes only to `app/parser.py`, `tests/test_parser.py`, and `README.md`.