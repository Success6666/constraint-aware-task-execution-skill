**Design**

Build a single-purpose `UrlMetadataFetcher` service with this contract:

```text
fetch(url, options) -> Result<Metadata, FetchError>
```

`Metadata` should contain:

```text
canonicalUrl
finalUrl
title
description
imageUrl
siteName
contentType
statusCode
fetchedAt
cacheStatus
```

Only `http` and `https` URLs are accepted. Reject credentials, fragments, unsupported ports, malformed URLs, and URLs exceeding a fixed length.

**Request policy**

Use conservative defaults:

```text
maxRedirects: 5
connectTimeout: 2s
tlsHandshakeTimeout: 3s
responseHeaderTimeout: 5s
totalTimeout: 10s
maxResponseBytes: 2 MiB
maxBodyReadTime: 5s
allowedPorts: 80, 443
```

Do not send a request body. Set a fixed, identifiable `User-Agent`. Disable proxy environment variables and application-configured proxies unless explicitly controlled and separately validated.

**SSRF protection**

Validate the destination before every network connection.

1. Parse the URL with a standards-compliant URL parser.
2. Reject IP literals that are not globally routable.
3. Resolve the hostname for both A and AAAA records.
4. Reject the request if any resolved address is:
   - Loopback
   - Unspecified
   - Link-local
   - Multicast
   - RFC1918 private IPv4
   - RFC6598 shared address space
   - IPv6 unique-local
   - IPv4-mapped IPv6 containing a blocked IPv4 address
   - Documentation, benchmarking, reserved, or otherwise non-global
   - A known cloud metadata address
5. Explicitly block common cloud metadata endpoints, including:
   - `169.254.169.254`
   - `169.254.170.2`
   - `100.100.100.200`
   - IPv6 link-local metadata targets such as `fe80::/10`
6. Treat DNS resolution failure, empty answers, malformed addresses, and ambiguous address families as failures.
7. Reject hostnames that resolve to any blocked address rather than selecting another answer.

The resolver and dialer must be integrated. Resolve once, validate the returned addresses, and connect directly to one of those validated addresses. Do not allow the HTTP library to independently resolve the hostname afterward, which prevents DNS rebinding between validation and connection. Preserve the original hostname for TLS SNI and the HTTP `Host` header while dialing the approved IP.

For every redirect, repeat the complete URL parsing, hostname resolution, address classification, port validation, and connection pinning process. Do not trust redirect destinations merely because the initial host was trusted. Reject redirects to different schemes, credentials, unsupported ports, or blocked destinations. Relative redirects are resolved against the current URL and then validated normally.

Disable automatic redirect handling in the underlying client and implement a redirect loop explicitly so each hop is observable and validated.

**Caching**

Use a bounded cache keyed by the canonical URL:

```text
scheme://lowercase-host[:effective-port]/normalized-path?query
```

Fragments are excluded. Preserve query parameter semantics unless the URL canonicalizer can normalize them without changing meaning.

Cache entries should include:

```text
metadata
storedAt
expiresAt
etag
lastModified
finalUrl
```

Use a configurable positive TTL, such as 10 minutes, and a short negative TTL, such as 30 seconds, for deterministic failures. Do not cache SSRF-policy failures for long periods because DNS and policy data can change.

Prevent cache stampedes with per-key request coalescing. Enforce maximum entry count and total cache size. Never cache credentials, response bodies, or sensitive response headers.

When an entry is stale, use conditional requests with `If-None-Match` and `If-Modified-Since` where available. On `304`, refresh the timestamp without reparsing the body. Optionally serve stale metadata for a short bounded window only for transient upstream failures, never for policy or validation failures.

**Response handling**

Accept only successful responses and selected redirects. Reject responses whose declared or observed body exceeds the limit. Stop reading immediately when the byte limit is reached.

Parse metadata from HTML using a bounded parser. Prefer:

1. `og:title`, `og:description`, `og:image`, `og:site_name`
2. Twitter card equivalents
3. `<title>` and relevant standard metadata

Resolve relative metadata URLs against the final response URL. Normalize whitespace, cap field lengths, and ignore malformed markup. Do not execute JavaScript, load subresources, follow embedded URLs, or fetch the declared image.

Validate the response `Content-Type` before parsing. Treat charset declarations defensively and normalize output to UTF-8. Do not log response bodies.

**Error model**

Expose stable error categories:

```text
invalid_url
unsupported_scheme
unsupported_port
blocked_address
dns_failure
connection_timeout
tls_failure
redirect_limit
redirect_rejected
response_timeout
body_too_large
unsupported_content_type
upstream_status
parse_failure
cache_failure
```

Include a safe human-readable message and internal diagnostic fields. Never include authorization headers, cookies, full query strings, response bodies, or sensitive DNS details in externally visible errors.

**Observability**

Emit structured logs containing:

```text
requestId
urlHost
scheme
redirectCount
cacheStatus
resultCategory
httpStatus
durationMs
bytesRead
resolvedAddressFamily
```

Log the host and a redacted URL path; omit or hash query values. Never log full headers or bodies.

Provide metrics for:

```text
fetch_requests_total{result,scheme,cache_status}
fetch_duration_seconds{result}
fetch_dns_duration_seconds
fetch_connect_duration_seconds
fetch_tls_duration_seconds
fetch_bytes_read_total
fetch_redirects_total
fetch_blocked_targets_total{reason}
fetch_cache_hits_total
fetch_cache_misses_total
fetch_inflight_requests
```

Create traces or spans for cache lookup, DNS resolution, connection, TLS, each redirect, response reading, and parsing. Record destination host and policy decision, but not sensitive URL components.

**Test plan**

Unit-test URL handling and address classification for:

- IPv4 and IPv6 literals
- IPv4-mapped IPv6
- Decimal, hexadecimal, and unusual IP representations
- `localhost` and equivalent hostnames
- RFC1918, RFC6598, loopback, link-local, multicast, unspecified, ULA, reserved, and metadata ranges
- Mixed DNS answers containing both public and blocked addresses
- DNS failures and empty answers
- Allowed public addresses on ports 80 and 443
- Credentials, fragments, unsupported schemes, and unsupported ports

Test the resolver/dialer integration to prove that:

- Validation occurs after DNS resolution.
- The dialer connects only to an approved resolved address.
- The hostname remains available for TLS SNI and `Host`.
- A DNS answer changing between calls cannot bypass validation.

Test redirect behavior with an in-memory HTTP server:

- Public to public redirect succeeds.
- Redirect to a private IP is rejected.
- Redirect to a hostname resolving privately is rejected.
- Redirect through multiple hops revalidates every hop.
- Redirect loops and redirect-limit exhaustion fail.
- Scheme, credential, and port changes are enforced.

Test timeouts, oversized bodies, malformed HTML, invalid encodings, unsupported content types, non-success statuses, conditional requests, `304` responses, stale fallback, cache expiry, cache eviction, and concurrent identical requests.

Add integration tests using a controllable DNS resolver and custom dialer. Avoid relying solely on public DNS or external cloud metadata services.

**Verification criteria**

The implementation is complete only when tests demonstrate that no request can be made to a blocked address through:

- Direct IP literals
- Hostnames resolving to blocked addresses
- Mixed A/AAAA results
- IPv4-mapped IPv6
- DNS rebinding
- Relative redirects
- Cross-host redirects
- Alternate numeric IP spellings
- Proxy configuration
- Automatic client-side redirect handling

Also verify that cache hits avoid network access, timeouts bound total work, response memory is capped, concurrent misses are coalesced, and observability data does not expose secrets or full query strings.
