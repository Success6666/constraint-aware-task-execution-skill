**Dependency License Policy**

1. **Allowed licenses**
   - MIT, BSD-2-Clause, BSD-3-Clause, ISC, Apache-2.0, Zlib, BSL-1.1, and other explicitly approved permissive licenses.
   - SPDX license expressions are the source of truth.
   - Dual-licensed dependencies are acceptable only when at least one permitted option is usable by the project.

2. **Restricted licenses**
   - GPL-2.0, GPL-2.0-only, GPL-2.0-or-later
   - GPL-3.0, GPL-3.0-only, GPL-3.0-or-later
   - AGPL-3.0, AGPL-3.0-only, AGPL-3.0-or-later
   - Any license expression containing GPL or AGPL is restricted unless covered by an approved exception.
   - LGPL, MPL, EPL, SSPL, source-available, custom, unknown, and `NOASSERTION` licenses require review. They are not automatically treated as GPL/AGPL, but should fail closed until explicitly classified.

3. **Scope**
   - Evaluate direct and transitive runtime and build dependencies.
   - Include production, development, test, optional, plugin, generated-code, and vendored dependencies unless a documented policy excludes a category.
   - License checks must run against the resolved dependency graph, not only manifest declarations.

4. **Exception requirements**
   Each exception must include:
   - Package coordinate and ecosystem.
   - Exact version or approved version range.
   - SPDX license identifier or expression.
   - Direct/transitive status.
   - Business and legal rationale.
   - Scope of permitted use.
   - Approver from Legal or the designated compliance owner.
   - Issue or ticket reference.
   - Owner.
   - Approval and expiration dates.
   - Optional checksum or lockfile identity for high-risk packages.

   Exceptions should be narrow. A package-wide wildcard or indefinite GPL/AGPL approval is prohibited.

Example exception record:

```yaml
exceptions:
  - id: EX-2025-001
    package: "example.org/tool"
    ecosystem: go
    versions:
      - "v2.4.1"
    license: "GPL-3.0-only"
    scope: "build-only"
    rationale: "Used only to generate development artifacts; absent from shipped binaries."
    owner: platform-team
    approver: legal@example.com
    ticket: LEGAL-1234
    approved_on: "2025-01-15"
    expires_on: "2025-12-31"
```

The enforcement engine must reject expired exceptions, malformed records, missing approvals, and exceptions whose package, version, license, or scope does not match the detected dependency.

**Required Outputs**

Every dependency scan produces:

- A CycloneDX or SPDX SBOM containing:
  - Package name, version, ecosystem, supplier, and PURL.
  - Declared and detected license expressions.
  - Dependency relationships.
  - Component scope.
  - Scan-tool version and generation timestamp.
- A machine-readable policy report containing:
  - Allowed components.
  - Restricted components.
  - Unknown or unclassified components.
  - Matched exception identifiers.
  - Violations and remediation guidance.
- A human-readable CI summary.
- A signed or tamper-evident SBOM artifact for every release build.

Use one SBOM format consistently across all ecosystems. CycloneDX JSON is a practical default; SPDX JSON is equally acceptable if already supported by organizational tooling.

**CI Workflow**

1. **Dependency resolution**
   - Resolve dependencies using locked, reproducible versions.
   - Fail if the dependency graph is not reproducible or the lock state is stale.

2. **License and SBOM generation**
   - Run an SPDX-aware license scanner over the complete resolved graph.
   - Generate the SBOM from the same resolved graph.
   - Normalize license identifiers to SPDX IDs and expressions.

3. **Policy evaluation**
   - Load the approved license policy and exception registry.
   - Match each component by ecosystem, package coordinate, resolved version, license expression, and scope.
   - Apply this decision order:
     1. Valid, unexpired matching exception: allow and report.
     2. GPL or AGPL without a matching exception: fail.
     3. Unknown, custom, or unresolved license: fail or require explicit classification.
     4. Approved license: allow.
     5. Other restricted license: fail pending review.

4. **Security and quality checks**
   - Run dependency vulnerability scanning separately from license scanning.
   - Verify the SBOM is valid against its schema.
   - Verify every resolved dependency appears in the SBOM.
   - Verify every SBOM component has a license result or an explicit unknown classification.
   - Upload the policy report and SBOM for pull requests and releases.
   - Publish release SBOMs alongside release provenance.

5. **Scheduled re-evaluation**
   - Run the complete scan on every pull request and protected branch push.
   - Run it on a scheduled basis, at least daily, to detect changed metadata, newly expired exceptions, and scanner database updates.
   - Run a release gate independently of pull-request checks.

Illustrative workflow logic:

```text
resolve locked dependencies
generate license inventory
generate SBOM
validate SBOM schema
evaluate policy

if any GPL/AGPL component lacks a valid exact exception:
    fail CI

if any unknown/custom/unresolved license lacks explicit approval:
    fail CI

if any exception is expired, malformed, or mismatched:
    fail CI

otherwise:
    publish SBOM and policy report
    pass CI
```

**Change-Control Rules**

- Adding or upgrading a dependency always triggers a full policy scan.
- Changing an exception requires review from both the owning engineering team and Legal/compliance.
- CI must prevent bypass through manually edited reports, because reports are generated from the resolved graph.
- Suppression comments in source code and ad hoc CI environment variables are not valid exceptions.
- A temporary emergency override may exist only as a protected, audited approval requiring an expiration timestamp and post-incident review.

**Tests**

The policy implementation should include automated fixtures for:

- Permissive direct dependency: passes.
- Permissive transitive dependency: passes.
- GPL direct dependency without exception: fails.
- GPL transitive dependency without exception: fails.
- AGPL dependency without exception: fails.
- GPL dependency with an exact valid exception: passes.
- GPL dependency with an exception for a different version: fails.
- GPL dependency with an expired exception: fails.
- GPL dependency with an exception for a different ecosystem or scope: fails.
- GPL-3.0-or-later and AGPL-3.0-or-later expressions: fail without exceptions.
- Dual license containing an approved option: passes according to the documented selection rule.
- Unknown, custom, malformed, and `NOASSERTION` licenses: fail pending classification.
- Multiple dependencies where only one violates policy: fails and identifies the exact component.
- Duplicate package versions where only one version is excepted: fails for the unapproved version.
- Missing or invalid SBOM relationships: fails validation.
- Missing license metadata: fails closed.
- Malformed exception records: fail before dependency evaluation.
- Exceptions whose approval or expiration dates are invalid: fail.
- Deterministic repeated scans produce equivalent component and policy results.

CI-level tests should also verify that:

- A newly introduced GPL dependency fails a pull request.
- Adding a valid exception makes the same dependency pass.
- Removing the exception causes the build to fail again.
- The generated SBOM contains the restricted dependency and its license.
- The CI summary names the package, version, license, dependency path, and required remediation.
- Release builds cannot publish without a passing policy decision and SBOM artifact.

**Required Failure Message**

A violation should be actionable:

```text
Dependency license policy violation:
  package: example.org/tool
  version: v2.4.1
  license: GPL-3.0-only
  dependency path: application -> build-helper -> example.org/tool
  exception: none
  action: remove, replace, or obtain an approved scoped exception
```

This design guarantees that any GPL or AGPL dependency introduced into the resolved graph fails CI unless a precise, approved, current exception explicitly permits it.
