# Contrastive Examples

## Banned Dependency

User: Build a FastAPI service without Redis.

Bad:

- Add Redis detection and compliance middleware.
- Fail startup or CI if Redis-related text is found.
- Center the final response on proving Redis absence.

Good:

- Build the working FastAPI service.
- Select a reasonable non-Redis design.
- Add no Redis-specific enforcement unless requested.
- Mention the chosen storage only when architecturally relevant.

## Banned Library

User: Do not use library X.

Bad: Create `LibraryXDetector`, `LibraryXGuard`, and `LibraryXPolicy`.

Good: Select another suitable library and complete the task normally.

## Soft Dependency Preference

User: Try not to add unnecessary dependencies.

Bad: Invent a dependency-count threshold and make the build fail above it.

Good: Reuse the standard library or existing dependencies where reasonable.

## Safety Exception

User: Accept file uploads but do not allow executable files.

Good: Validate file type and content because enforcement is required for safety and correctness.

The rule against unrequested gates does not remove necessary security boundaries.

## Explicit Enforcement

User: Do not use GPL dependencies, and make CI fail if one is introduced.

Good: Add the requested license scan and CI failure condition. The user explicitly asked for enforcement.

## Soft Preference Tradeoff

User: Avoid a database if practical, but preserve data across restarts.

Bad: Treat database avoidance as absolute and return a design that loses data.

Good: Explain briefly that persistence requires durable storage and choose the smallest suitable option.
