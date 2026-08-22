Design the policy as a repository-owned, reviewable contract. Generate an SBOM on every build, evaluate every direct and transitive component against that contract, and make exceptions explicit, scoped, approved, and expiring.

### Policy file

`policy/license-policy.yaml`:

```yaml
version: 1

blocked_spdx:
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

unknown_license: deny
unrecognized_license_expression: deny

exceptions:
  - purl: pkg:npm/example-gpl-package@1.2.3
    licenses:
      - GPL-3.0-only
    reason: "Required for legacy file conversion"
    owner: team-platform
    approved_by:
      - legal@example.com
      - security@example.com
    ticket: LEGAL-1234
    expires: 2027-06-30
```

Rules:

- Inspect direct and transitive dependencies.
- Parse SPDX identifiers and expressions structurally.
- A component is blocked if any license option in its expression is GPL or AGPL. For example, `MIT OR GPL-3.0-only` is blocked.
- An exception must match the exact package URL, version, and blocked license.
- Exceptions require a reason, owning team, legal/security approval, tracking ticket, and expiration date.
- Expired, malformed, or overly broad exceptions fail CI.
- Unknown or non-SPDX licenses fail CI until reviewed.
- Production and development dependencies are both checked unless the repository explicitly documents a narrower scope.

### SBOM and policy-checking flow

Use Syft (or an equivalent SBOM generator) to produce a CycloneDX or SPDX JSON document. Keep the policy evaluator separate from SBOM generation so the same checker can be used locally and in CI.

Recommended commands:

```text
syft dir:. -o cyclonedx-json=artifacts/sbom.cdx.json
license-policy check \
  --sbom artifacts/sbom.cdx.json \
  --policy policy/license-policy.yaml
```

The checker should emit a machine-readable report containing:

```json
{
  "status": "fail",
  "violations": [
    {
      "purl": "pkg:npm/bad-package@4.0.0",
      "licenses": ["GPL-3.0-only"],
      "exception": null
    }
  ]
}
```

### GitHub Actions workflow

`.github/workflows/license-policy.yml`:

```yaml
name: Dependency license policy

on:
  pull_request:
  push:
    branches: [main]
  schedule:
    - cron: "0 3 * * 1"

permissions:
  contents: read

jobs:
  licenses:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Install dependencies
        run: ./ci/install-dependencies.sh

      - name: Generate SBOM
        uses: anchore/sbom-action@v0
        with:
          path: .
          format: cyclonedx-json
          output-file: artifacts/sbom.cdx.json

      - name: Validate policy file
        run: ./ci/license-policy validate --policy policy/license-policy.yaml

      - name: Enforce dependency licenses
        run: |
          ./ci/license-policy check \
            --sbom artifacts/sbom.cdx.json \
            --policy policy/license-policy.yaml \
            --report artifacts/license-report.json

      - name: Run policy tests
        run: ./ci/license-policy test

      - name: Upload SBOM and report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: dependency-license-artifacts
          path: artifacts/
```

The `check` step must return a non-zero exit code for any unapproved GPL/AGPL dependency, so pull requests cannot merge.

### Tests

Store fixture SBOMs under `ci/license-policy/fixtures/` and test at least:

- MIT-only dependency: passes.
- Direct GPL dependency: fails.
- Transitive AGPL dependency: fails.
- GPL dependency with an exact valid exception: passes.
- Wrong version or package in an exception: fails.
- Expired exception: fails.
- Missing approver, ticket, owner, or reason: fails.
- `MIT OR GPL-3.0-only`: fails.
- `GPL-3.0-only AND MIT`: fails.
- Unknown license: fails.
- Multiple violations produce all reported violations.
- A newly introduced dependency is detected by comparing the current SBOM with the base revision.

Run these tests both in CI and locally through the same `license-policy test` command.

Require CODEOWNERS approval from the security and legal owners for changes to `policy/license-policy.yaml`. Add a scheduled job so newly released dependency versions are re-evaluated even when application code has not changed.