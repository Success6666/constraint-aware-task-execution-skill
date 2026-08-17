# Validation Contracts

Validation is runtime infrastructure, not a component added to the user's deliverable merely to demonstrate compliance.

## Allowed checks

- file existence and allowed path scope;
- JSON shape, exact values, and supported schema keywords;
- Markdown heading contracts;
- Python AST, compilation, and import restrictions;
- JavaScript syntax and optional YAML parsing when the required runtime is available;
- explicit forbidden patterns;
- commands that match the runtime allowlist and execute without a shell.

The validator registry must reject path traversal and arbitrary command execution. A model-proposed validator name is data; it does not authorize loading code or running a command.

## Repair feedback

Return validator IDs, paths, and machine error codes. Do not return benchmark scores, over-optimization counts, capability scores, or instructions to make the response resemble a reference answer.

After a repair, re-run path scope and every applicable contract. Previously passing checks are evidence, not permanent exemptions.

Generic semantic correctness cannot be proven by syntax, AST, marker, length, or keyword checks. Report it as `partial` or `unsupported` unless the case declares a deterministic contract.

