## License Policy

**Default rule:** All direct and transitive dependencies are scanned. A dependency whose effective SPDX license expression requires GPL or AGPL compliance is denied unless it matches an active, approved exception.

Denied license families:

- `GPL-1.0-only`, `GPL-1.0-or-later`
- `GPL-2.0-only`, `GPL-2.0-or-later`
- `GPL-3.0-only`, `GPL-3.0-or-later`
- `AGPL-1.0-only`, `AGPL-1.0-or-later`
- `AGPL-3.0-only`, `AGPL-3.0-or-later`
- Deprecated SPDX aliases after normalization

Use an SPDX-aware expression parser. Do not detect licenses with substring or regular-expression matching.

Expression behavior:

- `MIT OR GPL-3.0-only`: allowed when the project may select MIT.
- `MIT AND GPL-3.0-only`: denied.
- `GPL-2.0-only WITH Classpath-exception-2.0`: denied unless policy explicitly recognizes that exception or an approved package exception exists.
- `NOASSERTION` and unknown licenses: reported as warnings initially, with an option to make them blocking later.

## Repository Layout

```text
compliance/
  license-policy.yaml
  license-exceptions.yaml
  README.md
scripts/
  check-licenses
tests/
  fixtures/sbom/
    allowed.json
    gpl-direct.json
    agpl-transitive.json
    dual-license-or.json
    dual-license-and.json
    approved-exception.json
    expired-exception.json
.github/
  workflows/license-compliance.yml
  CODEOWNERS
artifacts/
  # CI-generated only; not committed
```

Example policy:

```yaml
version: 1

deny:
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

unknown_license: warn
exception_file: compliance/license-exceptions.yaml
```

Exceptions should be narrow and auditable:

```yaml
version: 1

exceptions:
  - id: LIC-2026-004
    package: pkg:npm/example-package
    versions: ">=2.4.1 <2.5.0"
    licenses:
      - GPL-3.0-only
    scopes:
      - development
    justification: Used only by an internal build-time tool and not distributed.
    obligations: Do not include the package in production artifacts.
    owner: developer-platform
    approval_ticket: LEGAL-1842
    approved_by: legal-compliance
    approved_on: 2026-08-10
    expires_on: 2026-11-10
```

An exception matches only when package URL, version, license, and scope all match and the expiry date has not passed. Avoid package-name wildcards and unbounded version ranges.

## CI Workflow

The workflow should:

1. Install dependencies using locked or frozen resolution.
2. Build distributable artifacts or container images.
3. Generate a CycloneDX JSON SBOM containing direct and transitive dependencies.
4. Generate a second SBOM from the final container or distributable artifact when applicable.
5. Validate the SBOM schema.
6. Evaluate every component’s SPDX expression against the policy and exceptions.
7. Upload the SBOM and machine-readable compliance report even when policy evaluation fails.
8. Exit nonzero for every unexcepted GPL or AGPL finding.

Conceptual GitHub Actions job:

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

      - name: Install locked dependencies
        run: ./scripts/install-dependencies --frozen

      - name: Build production artifact
        run: ./scripts/build

      - name: Generate SBOM
        run: |
          syft dir:. -o cyclonedx-json=workspace.sbom.json
          syft ./dist -o cyclonedx-json=artifact.sbom.json

      - name: Enforce license policy
        run: |
          ./scripts/check-licenses \
            --policy compliance/license-policy.yaml \
            --exceptions compliance/license-exceptions.yaml \
            --sbom workspace.sbom.json \
            --sbom artifact.sbom.json \
            --report license-report.json

      - name: Upload compliance evidence
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: license-compliance
          path: |
            *.sbom.json
            license-report.json
```

The checker’s failure output should identify the package URL, resolved version, dependency scope, detected SPDX expression, dependency path, and the reason no exception matched.

Protect the enforcement boundary with branch protection and ownership:

```text
/compliance/license-policy.yaml       @legal-compliance
/compliance/license-exceptions.yaml   @legal-compliance
/scripts/check-licenses               @legal-compliance @developer-platform
/.github/workflows/license-compliance.yml @legal-compliance @developer-platform
```

Require the `license-compliance` status check and CODEOWNER approval before merging.

## Tests

The policy checker should have fixture-based tests proving:

- MIT, Apache-2.0, BSD, and ISC dependencies pass.
- Direct GPL dependencies fail.
- Transitive AGPL dependencies fail.
- SPDX `OR` expressions pass when a permitted licensing choice exists.
- SPDX `AND` expressions fail when a denied obligation remains.
- An exact approved exception passes.
- Wrong package, version, license, or scope does not match an exception.
- Expired and malformed exceptions fail.
- Missing or malformed SBOMs fail closed.
- Multiple SBOMs are deduplicated without hiding violations.
- Exit codes and JSON reports remain stable for CI consumers.

Run these tests in the same required workflow before scanning the project. This makes the enforcement logic itself a tested part of the merge gate, while the generated SBOM and report provide review and audit evidence.