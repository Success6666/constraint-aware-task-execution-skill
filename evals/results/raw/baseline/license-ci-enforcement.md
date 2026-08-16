Use a deny-by-default policy evaluator over a generated CycloneDX SBOM. Keep exceptions version-scoped, reviewed, and expiring.

**Repository layout**

```text
compliance/
  license-policy.yml
  license-exceptions.yml
  evaluate_licenses.py
  schemas/
    exception.schema.json
tests/
  fixtures/
    allowed.cdx.json
    gpl.cdx.json
    agpl-transitive.cdx.json
    approved-exception.cdx.json
  test_license_policy.py
.github/workflows/license-compliance.yml
artifacts/
  sbom.cdx.json
  license-report.json
```

**Policy**

```yaml
# compliance/license-policy.yml
version: 1

deny:
  spdx_ids:
    - GPL-1.0-only
    - GPL-1.0-or-later
    - GPL-2.0-only
    - GPL-2.0-or-later
    - GPL-3.0-only
    - GPL-3.0-or-later
    - AGPL-1.0-only
    - AGPL-1.0-or-later
    - AGPL-3.0-only
    - AGPL-3.0-or-later

  # Normalize deprecated identifiers before evaluation.
  aliases:
    GPL-2.0: GPL-2.0-only
    GPL-3.0: GPL-3.0-only
    AGPL-3.0: AGPL-3.0-only

fail_on:
  missing_license: true
  invalid_spdx_expression: true
  expired_exception: true
```

SPDX expressions must be parsed structurally:

- `MIT OR GPL-3.0-only` passes because a permitted licensing option exists.
- `MIT AND GPL-3.0-only` fails.
- `GPL-2.0-only WITH Classpath-exception-2.0` still fails unless that exact expression has an approved exception.
- Both direct and transitive dependencies are evaluated.

**Exceptions**

```yaml
# compliance/license-exceptions.yml
version: 1
exceptions:
  - id: LIC-2026-004
    package:
      purl: pkg:npm/example@2.4.1
    license_expression: GPL-3.0-only
    scope:
      environments: [development]
    justification: Used only by an isolated build-time documentation process.
    owner: developer-experience
    approved_by: legal@example.com
    approved_on: 2026-08-10
    expires_on: 2026-11-10
    issue: https://issues.example.com/LIC-2026-004
```

An exception should match the exact package URL, version, license expression, and environment. Wildcard packages or versions should be rejected. Changes to this file should require CODEOWNERS approval from Legal or Open Source Program Office.

**Evaluator contract**

The evaluator receives the CycloneDX JSON SBOM and the two policy files. It should:

1. Validate policy and exception schemas.
2. Inspect every component, including transitives.
3. Normalize and parse SPDX expressions.
4. Determine whether each expression permits a non-denied licensing choice.
5. Match violations against active, exact exceptions.
6. Emit a deterministic JSON report.
7. Exit nonzero for unexcepted GPL/AGPL, missing licenses, malformed expressions, or invalid exceptions.

Example report:

```json
{
  "result": "failed",
  "violations": [
    {
      "purl": "pkg:npm/bad-package@1.2.0",
      "license": "AGPL-3.0-only",
      "dependencyType": "transitive",
      "reason": "denied license without approved exception"
    }
  ],
  "exceptionsUsed": [],
  "unusedExceptions": ["LIC-2026-004"]
}
```

**GitHub Actions workflow**

```yaml
name: License compliance

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  license-compliance:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install locked project dependencies
        run: ./scripts/ci-install-dependencies.sh

      - name: Generate CycloneDX SBOM
        run: |
          syft dir:. \
            --scope all-layers \
            --output cyclonedx-json=artifacts/sbom.cdx.json

      - name: Validate and evaluate licenses
        run: |
          python compliance/evaluate_licenses.py \
            --sbom artifacts/sbom.cdx.json \
            --policy compliance/license-policy.yml \
            --exceptions compliance/license-exceptions.yml \
            --report artifacts/license-report.json

      - name: Run policy tests
        run: python -m pytest tests/test_license_policy.py

      - name: Upload compliance evidence
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: license-compliance-${{ github.sha }}
          path: |
            artifacts/sbom.cdx.json
            artifacts/license-report.json
```

Pin third-party actions and the SBOM generator by immutable commit or verified version in production. Generate the SBOM after a lockfile-enforced install so resolved transitive versions are represented.

**Required tests**

| Fixture | Expected result |
|---|---|
| MIT/Apache dependencies | Pass |
| Direct GPL dependency | Fail |
| Transitive AGPL dependency | Fail |
| Exact approved exception | Pass and report exception usage |
| Exception for another version | Fail |
| Expired exception | Fail |
| Wildcard exception | Schema failure |
| `MIT OR GPL-3.0-only` | Pass |
| `MIT AND GPL-3.0-only` | Fail |
| Deprecated GPL identifier | Normalize and fail |
| Missing license | Fail |
| Invalid SPDX expression | Fail |
| Package removed but exception remains | Pass with unused-exception warning |

Make the workflow a required branch-protection check named `License compliance / license-compliance`. That prevents merging any dependency change that introduces GPL or AGPL without a valid, approved exception.