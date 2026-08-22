# Dependency License Policy and CI Design

## 1. Policy objectives

The policy applies to every resolved dependency, including:

- Direct and transitive dependencies
- Runtime, build, test, and optional dependencies
- Dependencies included in release artifacts
- Dependencies introduced through plugins or generated code

CI must fail if any resolved dependency is licensed under GPL or AGPL without a valid approved exception.

## 2. License rules

### Always permitted

Permissive licenses may be approved by default, including:

- MIT
- BSD-2-Clause and BSD-3-Clause
- Apache-2.0
- ISC
- Zlib
- Unlicense
- Public-domain equivalents

### Prohibited without exception

The following license families require an exception:

- GPL-1.0, GPL-2.0, GPL-3.0
- GPL-2.0-only, GPL-2.0-or-later
- GPL-3.0-only, GPL-3.0-or-later
- AGPL-1.0, AGPL-3.0
- AGPL-3.0-only, AGPL-3.0-or-later

Both `WITH` variants and recognized SPDX license expressions must be handled.

The default rule is conservative: if dependency metadata indicates a GPL or AGPL license and no approved exception matches, CI fails. A dual-license dependency is not automatically accepted unless the permissible licensing branch is explicitly verified and recorded.

### Unknown or missing licenses

Unknown, malformed, or missing license metadata must fail policy review unless a separate approved metadata exception exists. This prevents dependencies from bypassing the GPL/AGPL check through incomplete metadata.

## 3. Exception model

Each exception must contain:

```yaml
exceptions:
  - dependency: "vendor/package"
    version: "2.4.1"
    license: "GPL-3.0-only"
    scope: "build-only"
    reason: "Required by the release toolchain"
    approver: "legal@example.com"
    approval_ticket: "LEGAL-1234"
    approved_at: "2025-01-15"
    expires_at: "2025-07-15"
    owner: "team-name"
```

Required fields:

- Exact dependency identifier
- Exact resolved version, or a narrowly bounded version range
- SPDX license identifier
- Usage scope
- Business and technical justification
- Named approver
- Approval or tracking reference
- Expiration date
- Owning team

Rules:

1. Exceptions are deny-by-default and apply only to the named dependency and license.
2. An exception must not cover an entire ecosystem, organization, or unbounded version range.
3. Expired exceptions fail CI.
4. A changed version, license, dependency scope, or package identity requires a new approval.
5. Exceptions must be reviewed at least annually.
6. Exceptions for production or distributed dependencies require legal approval.
7. Build-only exceptions must not permit the dependency into a production SBOM or release artifact.
8. An exception does not suppress SBOM generation or reporting.

## 4. Policy evaluation

The license evaluator should:

1. Resolve the complete dependency graph from the lock state used by CI.
2. Normalize package names, versions, and SPDX identifiers.
3. Read license data from package metadata and authoritative package manifests.
4. Evaluate every resolved dependency.
5. Classify each dependency as:
   - Allowed
   - Allowed by exception
   - Prohibited
   - Unknown or unverifiable
6. Fail if any dependency is prohibited without a matching, unexpired exception.
7. Fail if license metadata is missing or ambiguous.
8. Produce a machine-readable decision report containing:
   - Dependency
   - Version
   - Direct or transitive status
   - License expression
   - Classification
   - Matching exception, if any
   - Dependency path introducing it

The policy must evaluate the actual resolved graph rather than only direct dependency declarations.

## 5. SBOM requirements

CI must generate an SBOM after dependency resolution and before release publication.

The SBOM should use SPDX or CycloneDX and include:

- Package name and version
- Package identifier and ecosystem
- SPDX license expression
- License evidence and source
- Dependency relationships
- Direct/transitive classification
- Package checksum
- Source or distribution URL where available
- Build metadata
- Creation timestamp
- Commit identifier
- Application version

The SBOM must represent the release dependency graph, not merely the top-level manifest. It must be retained as a CI artifact and attached to every release.

The license policy report and SBOM must reference the same resolved dependency set. CI should fail if they differ.

## 6. CI workflow

### Pull request and branch CI

Run these stages in order:

1. **Dependency resolution**
   - Use the lock state.
   - Reject unexpected lock changes.
   - Resolve direct and transitive dependencies deterministically.

2. **Dependency tests**
   - Run the normal unit and integration test suite.

3. **SBOM generation**
   - Generate a complete SBOM from the resolved graph.
   - Validate SBOM syntax and required fields.

4. **License policy evaluation**
   - Normalize and evaluate all dependency licenses.
   - Apply the exception list.
   - Fail on unapproved GPL/AGPL dependencies.
   - Fail on expired or malformed exceptions.
   - Fail on unknown or missing licenses.

5. **SBOM and policy consistency**
   - Confirm every resolved dependency appears in the SBOM.
   - Confirm every SBOM dependency was policy-evaluated.
   - Confirm package versions and checksums match the lock state.

6. **Review reporting**
   - Publish the SBOM, policy report, and dependency-license summary.
   - Clearly identify the dependency path causing every failure.

### Release CI

Release CI must additionally:

- Re-run dependency resolution from a clean environment.
- Re-run all license checks; never rely only on pull-request results.
- Generate the final release SBOM.
- Verify that no build-only exception is present in the production dependency graph.
- Attach the final SBOM and policy report to the release.
- Block publication if any policy stage fails.

## 7. Required tests

### Policy-engine unit tests

Include fixtures for:

- GPL-2.0-only without exception: fails
- GPL-3.0-or-later without exception: fails
- AGPL-3.0-only without exception: fails
- GPL dependency with an exact valid exception: passes
- AGPL dependency with an exact valid exception: passes
- Exception for a different version: fails
- Exception for a different package: fails
- Expired exception: fails
- Missing approver or ticket: fails
- Unbounded version exception: fails
- Missing license metadata: fails
- Invalid SPDX expression: fails
- Permissive dependency: passes
- Transitive prohibited dependency: fails
- Multiple prohibited dependencies: reports all failures
- Build-only exception used by a production dependency: fails

### Workflow integration tests

Verify that:

1. Adding a direct GPL dependency causes CI failure.
2. Adding a transitive GPL dependency causes CI failure.
3. Adding an AGPL dependency causes CI failure.
4. Adding a valid, unexpired exception allows the intended dependency.
5. Changing the exempted version causes failure.
6. Removing the exception causes failure.
7. The SBOM includes all resolved dependencies.
8. The policy report and SBOM contain identical package/version/checksum sets.
9. A release cannot proceed when the license gate fails.
10. The generated SBOM is valid and reproducible from the same lock state.

## 8. Ownership and maintenance

- Engineering owns dependency resolution and SBOM accuracy.
- Legal or designated compliance reviewers approve GPL/AGPL exceptions.
- Each exception has a technical owner.
- CI reports exceptions approaching expiration.
- Dependency upgrades must re-run license evaluation even when the package name is unchanged.

The enforcement invariant is:

> Every resolved GPL or AGPL dependency must either be absent, explicitly approved by a matching unexpired exception, or cause CI to fail.
