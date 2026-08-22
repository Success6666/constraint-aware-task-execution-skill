Implement the parser change in three coordinated parts:

1. **`app/parser.py`**
   - Preserve the current public API and valid-input behavior.
   - Add explicit validation around malformed input before existing parsing logic runs.
   - Define a consistent failure policy, preferably a dedicated parser exception (or the project’s existing exception type) with a concise, actionable message.
   - Handle likely malformed cases explicitly: missing required fields, invalid delimiters/structure, empty input, incorrect types, and truncated records.
   - Ensure partial or ambiguous data is rejected rather than silently normalized.
   - Keep compatibility by retaining existing return shapes, accepted syntax, and exception behavior for inputs that were previously valid.

2. **`tests/test_parser.py`**
   - Add regression tests covering every newly handled malformed-input category.
   - Add tests asserting the exact exception type and, where useful, stable message fragments.
   - Add compatibility tests for representative valid inputs, including boundary cases already supported by the parser.
   - Include a regression test for the original failure mode, proving malformed data now fails deterministically without affecting neighboring records or parser state.
   - Keep tests focused on the public parser contract rather than implementation details.

3. **`README.md`**
   - Document the accepted input format and required fields.
   - Add a short “Malformed input” section describing rejection behavior, exception type, and whether callers should catch it.
   - Include one valid example and one invalid example.
   - Note that existing valid inputs and return values remain backward-compatible.

Suggested implementation sequence:

- First codify current valid behavior with characterization tests.
- Add validation and normalized error handling in `app/parser.py`.
- Add malformed-input and regression coverage.
- Update README examples and error-handling guidance.
- Run the parser test suite and verify all existing tests remain green.

The key compatibility rule is: **only newly invalid or previously undefined inputs should change behavior; all established valid inputs must produce the same outputs as before.**