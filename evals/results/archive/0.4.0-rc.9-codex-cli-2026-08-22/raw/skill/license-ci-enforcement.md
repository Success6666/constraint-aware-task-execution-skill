## Dependency License Policy

- Permitted by default: permissive licenses such as MIT, BSD-2-Clause/3-Clause, Apache-2.0, ISC, and 0BSD.
- GPL and AGPL licenses are prohibited unless an explicit, unexpired exception exists.
- Detect both direct and transitive dependencies from the lockfile.
- Normalize SPDX identifiers and license expressions before evaluation.
- Unknown, missing, or ambiguous licenses fail CI for manual review.
- Exceptions must identify the exact package and version, rationale, owner, approval ticket, and expiration date.

Example `license-policy.yml`:

```yaml
default: deny-unknown

deny:
  - GPL-2.0
  - GPL-2.0-only
  - GPL-2.0-or-later
  - GPL-3.0
  - GPL-3.0-only
  - GPL-3.0-or-later
  - AGPL-3.0
  - AGPL-3.0-only
  - AGPL-3.0-or-later

exceptions:
  - package: example-gpl-library
    version: 2.4.1
    license: GPL-3.0-only
    reason: "Required for legacy data import"
    owner: platform-team
    approval: LEGAL-1427
    expires: 2027-01-31
```

Exceptions should be exact-version entries; wildcard versions should be disallowed unless separately approved.

## CI Workflow

Use Syft to produce a CycloneDX or SPDX SBOM, then run a policy checker against it.

```yaml
name: dependency-license-policy

on:
  pull_request:
  push:
    branches: [main]
  schedule:
    - cron: "0 3 * * 1"

jobs:
  licenses:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Generate SBOM
        uses: anchore/sbom-action@v0
        with:
          format: cyclonedx-json
          output-file: sbom.cdx.json
          artifact-name: dependency-sbom

      - name: Validate dependency licenses
        run: |
          python ci/check_licenses.py \
            --sbom sbom.cdx.json \
            --policy license-policy.yml \
            --lockfile-diff "${{ github.event.before }}...${{ github.sha }}"

      - name: Upload SBOM
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: dependency-sbom
          path: sbom.cdx.json
```

The checker should:

1. Read every component and its SPDX license expression.
2. Canonicalize equivalent identifiers.
3. Reject GPL/AGPL components without a matching, non-expired exception.
4. Reject unknown or malformed licenses.
5. On pull requests, report newly introduced violations prominently.
6. Exit nonzero so the required status check blocks merging.
7. Emit dependency, version, detected license, exception status, and remediation guidance in the CI log.
8. Revalidate all dependencies on scheduled builds so expired exceptions are caught.

Protect the default branch by requiring the `licenses` job to pass before merge.

## Tests

Add policy-checker tests covering:

- MIT, BSD, Apache, and ISC dependencies pass.
- GPL-2.0, GPL-3.0, and AGPL-3.0 dependencies fail.
- An exact approved exception passes.
- An expired exception fails.
- A version mismatch fails.
- A package-name mismatch fails.
- Transitive GPL/AGPL dependencies fail.
- SPDX expressions such as `GPL-3.0-only OR MIT` follow the organization’s chosen rule, preferably deny unless legal approval covers the expression.
- Missing, unknown, and malformed license metadata fails.
- Duplicate exceptions and wildcard versions fail policy validation.
- A lockfile fixture introducing a prohibited dependency causes a nonzero exit.
- A fixture removing the dependency passes.
- SBOM output is generated and contains the expected component and license fields.

Use small checked-in fixtures for allowed, denied, expired-exception, unknown-license, and transitive-dependency cases. The CI workflow itself should be tested with a YAML/linter check and one integration test invoking the checker against the denied fixture.