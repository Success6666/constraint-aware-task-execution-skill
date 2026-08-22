Design a small, policy-driven fetch service with a hardened HTTP client.

### API

```text
fetchMetadata(url, options) -> {
  finalUrl,
  status,
  contentType,
  title,
  description,
  canonicalUrl,
  fetchedAt,
  fromCache
}
```

Accept only `http` and `https`. Reject malformed URLs, credentials, unsupported ports, and excessive URL length.

### Request flow

1. Normalize the URL and derive a cache key from scheme, hostname, effective port, and path/query.
2. Check the bounded TTL cache.
3. Resolve the hostname with both IPv4 and IPv6 support.
4. Reject the request if any returned address is unsafe:
   - IPv4 loopback, private, link-local, unspecified, multicast, reserved, and CGNAT ranges.
   - IPv6 loopback, unspecified, ULA, link-local, multicast, and IPv4-mapped unsafe addresses.
   - Cloud metadata endpoints, including `169.254.169.254`; link-local blocking covers equivalent metadata targets.
5. Connect only to the validated resolved address to prevent DNS rebinding. For HTTPS, retain the original hostname for SNI and certificate validation while dialing the validated IP.
6. Disable automatic redirects. For every `Location`:
   - Resolve the new hostname again.
   - Reapply the complete IP policy.
   - Reapply scheme, port, and redirect-count limits.
   - Repeat the pinned connection process.
7. Enforce response limits:
   - Maximum redirect count.
   - Maximum body size.
   - Maximum header size.
   - Reject non-text content unless explicitly supported.
8. Parse metadata with tolerant HTML parsing and return normalized fields.

A redirect from a public hostname to a private address must fail before connection. DNS answers containing both public and blocked addresses should fail the request rather than selecting an apparently safe answer.

### Timeouts

Use separate, bounded deadlines for:

- DNS lookup
- TCP connect
- TLS handshake
- Response headers
- Total request
- Body read

Propagate cancellation through the entire operation. Never retry unsafe or malformed requests. Any retry policy should be limited to transient transport failures and remain within the total deadline.

### Caching

Use an in-memory or shared cache with:

- Configurable TTL and maximum entries/bytes
- LRU eviction
- Cache key based on normalized URL
- Cached success responses only by default
- Optional short negative-cache TTL for deterministic failures
- Stale-on-error only when explicitly enabled
- Per-key request coalescing to prevent stampedes

Do not cache authorization headers, cookies, or raw bodies unless required.

### Observability

Emit structured logs, metrics, and traces containing:

- Request ID and normalized host
- Cache hit/miss and eviction counts
- DNS, connect, TLS, header, and total durations
- Redirect count
- Final status and content type
- Response-size and parse outcomes
- Rejection reason category

Redact query values, credentials, authorization data, and response contents. Use stable error codes such as `blocked_ip`, `dns_failure`, `redirect_limit`, `timeout`, and `body_too_large`.

### Tests

Cover:

- Public IPv4 and IPv6 success cases
- Every blocked IPv4/IPv6 range
- Cloud metadata addresses
- Hostnames resolving to mixed safe and unsafe addresses
- DNS rebinding between initial request and redirect
- Redirects to private, loopback, link-local, metadata, unsupported-scheme, and excessive-hop targets
- IP pinning after validation
- HTTPS SNI and certificate validation
- Each timeout phase and cancellation
- Cache hits, expiry, eviction, negative caching, and concurrent coalescing
- Oversized headers/bodies and malformed HTML
- Correct redaction and emitted observability fields

The key implementation rule is: never let the underlying client follow redirects or perform an unvalidated hostname connection. Every hop must undergo fresh DNS resolution, IP classification, and a pinned connection.