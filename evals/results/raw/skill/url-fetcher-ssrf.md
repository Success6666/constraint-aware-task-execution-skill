# URL Metadata Fetcher Design

## 1. Public API

```text
fetchMetadata(url, options) -> Result<Metadata, FetchError>
```

```text
Metadata {
  url: string              // final URL
  title: string?
  description: string?
  image: string?
  siteName: string?
  contentType: string
  fetchedAt: timestamp
  fromCache: boolean
}
```

Configurable options:

```text
FetchOptions {
  totalTimeout: duration       // default 10s
  dnsTimeout: duration         // default 2s
  connectTimeout: duration     // default 2s
  tlsTimeout: duration         // default 3s
  headerTimeout: duration      // default 3s
  bodyTimeout: duration        // default 5s
  maxRedirects: integer        // default 5
  maxResponseBytes: integer    // default 1 MiB
  cacheTtl: duration           // default 5 minutes
}
```

Only `http` and `https` URLs are accepted. Reject:

- Missing or invalid host
- Userinfo (`https://user:pass@host`)
- Unsupported schemes
- Explicitly disallowed ports, if the deployment requires only 80 and 443
- URLs exceeding configured length limits

## 2. Request flow

1. Parse and normalize the URL.
2. Check the cache using the normalized URL.
3. Create one overall deadline covering DNS, connection, TLS, headers, body, and redirects.
4. For each URL:
   1. Validate the scheme and hostname.
   2. Resolve the hostname with the configured resolver.
   3. Validate every returned address.
   4. Reject the request if any address is disallowed.
   5. Connect only to one of the validated resolved addresses.
   6. Send the request with automatic redirects disabled.
   7. If the response is a redirect, resolve the `Location` header against the current URL and repeat the complete validation process.
5. Limit response size and parse metadata.
6. Cache successful results.
7. Return the metadata and final URL.

Redirects must not be delegated to the HTTP client because each redirect requires a fresh DNS resolution and address-policy check.

## 3. SSRF and target validation

### Address policy

Reject every resolved address in these categories:

#### IPv4

- Loopback: `127.0.0.0/8`
- Private:
  - `10.0.0.0/8`
  - `172.16.0.0/12`
  - `192.168.0.0/16`
- Link-local: `169.254.0.0/16`
- Unspecified: `0.0.0.0/8`
- Multicast: `224.0.0.0/4`
- Reserved and benchmarking ranges
- Carrier-grade NAT: `100.64.0.0/10`
- Cloud metadata addresses, including:
  - `169.254.169.254`
  - `100.100.100.200`

#### IPv6

- Loopback: `::1/128`
- Unspecified: `::/128`
- Link-local: `fe80::/10`
- Unique local/private: `fc00::/7`
- Multicast: `ff00::/8`
- IPv4-mapped IPv6 addresses whose embedded IPv4 address is disallowed
- Known cloud metadata addresses, including `fd00:ec2::254`

The policy should be implemented using a standard IP-network library rather than string matching.

### Hostname policy

Also reject known metadata hostnames and internal service names, such as:

- `metadata.google.internal`
- `metadata`
- `instance-data`
- Configured organization-specific internal suffixes

Hostname checks are defense in depth; address checks remain mandatory after DNS resolution.

### DNS rebinding protection

The resolver and connector must be coordinated:

- Resolve the hostname explicitly.
- Validate all returned addresses.
- Dial one of those exact validated IP addresses.
- Do not perform a second unconstrained hostname lookup during connection.
- Disable proxy use and environment-provided proxy settings.
- Preserve the original hostname for the HTTP `Host` header and TLS SNI while dialing the validated IP.
- Avoid reusing an existing connection for a new validation cycle, or ensure connection pooling is keyed by validated destination and policy context.

If DNS returns both allowed and disallowed addresses, reject the hostname rather than selecting only the allowed result.

A literal IP URL is validated directly without DNS.

## 4. Redirect handling

Use a manual redirect loop:

```text
current = initialUrl
visited = set()

for hop in 0..maxRedirects:
    normalized = normalize(current)

    if normalized in visited:
        return RedirectLoop
    visited.add(normalized)

    resolvedAddresses = resolve(normalized.host, dnsDeadline)
    validateAll(resolvedAddresses)
    response = requestUsingPinnedAddresses(normalized, resolvedAddresses)

    if response.status is not redirect:
        return response

    location = response.headers["Location"]
    if location is missing:
        return InvalidRedirect

    next = resolveRelativeUrl(normalized, location)
    validateSchemeAndUrlSyntax(next)
    current = next

return TooManyRedirects
```

For each redirect:

- Resolve relative locations against the current URL.
- Re-run scheme, hostname, DNS, and IP validation.
- Reject redirects to unsupported schemes, including `file:`, `ftp:`, `gopher:`, and `data:`.
- Reject credentials in the redirected URL.
- Enforce a maximum redirect count.
- Detect redirect loops using normalized URLs.

## 5. HTTP behavior

Use:

- `GET`
- A bounded, explicit `User-Agent`
- `Accept: text/html, application/xhtml+xml`
- Automatic decompression only if decompressed size is also limited
- No request body
- No cookies or ambient authentication
- No proxy
- No credential forwarding across redirects

Only parse responses with an HTML-compatible content type unless the product explicitly supports other formats.

Enforce:

- Maximum response body bytes
- Maximum header bytes
- Maximum HTML nesting or parser work, where supported
- Cancellation when any timeout or caller cancellation occurs

Treat only successful responses, normally HTTP `200`, as cacheable metadata sources. Do not cache timeout, DNS, policy, redirect, or transport failures unless a separate short-lived negative-cache policy is intentionally added.

## 6. Metadata extraction

Parse the bounded HTML document and use this precedence:

### Title

1. `meta[property="og:title"]`
2. `meta[name="twitter:title"]`
3. `<title>`

### Description

1. `meta[property="og:description"]`
2. `meta[name="description"]`
3. `meta[name="twitter:description"]`

### Image

1. `meta[property="og:image"]`
2. `meta[name="twitter:image"]`

Resolve relative image URLs against the final page URL. Do not fetch the image as part of metadata extraction unless that is a separate explicitly authorized operation.

Normalize extracted text by decoding entities, trimming whitespace, and applying field length limits. Ignore malformed or duplicate tags after the first valid value.

## 7. Caching

Use a bounded, concurrency-safe cache:

```text
CacheEntry {
  metadata: Metadata
  expiresAt: timestamp
}
```

Cache key:

- Lowercase scheme and hostname
- Remove default ports
- Normalize path
- Preserve query parameters because they can change content
- Exclude fragments because fragments are not sent to the server

Recommended behavior:

- Positive TTL: 5 minutes by default
- Size limit by entry count and total bytes
- LRU or equivalent eviction
- Single-flight request coalescing so concurrent requests for the same key share one fetch
- Return a cache hit without performing DNS or network access
- Do not cache results produced after a timeout or policy failure

Cache entries must not contain credentials or unbounded response content.

## 8. Timeouts

Apply both individual and overall deadlines:

| Operation | Default |
|---|---:|
| Total fetch | 10s |
| DNS resolution | 2s |
| TCP connect | 2s |
| TLS handshake | 3s |
| Response headers | 3s |
| Response body | 5s |

The total deadline is authoritative. A redirect does not reset it.

Every blocking operation must accept cancellation from the overall deadline and caller cancellation.

## 9. Error model

Return stable, machine-readable categories:

```text
InvalidUrl
UnsupportedScheme
BlockedAddress
BlockedHostname
DnsFailure
DnsTimeout
ConnectTimeout
TlsFailure
HeaderTimeout
BodyTimeout
ResponseTooLarge
TooManyRedirects
RedirectLoop
HttpError
InvalidResponse
ParseFailure
Cancelled
```

Do not expose internal resolver details or response bodies in normal error messages.

## 10. Observability

Emit structured metrics:

- Fetch attempts, successes, failures
- Cache hits, misses, evictions
- Fetch latency, DNS latency, connection latency, TLS latency
- Redirect count
- Response sizes
- Metadata parse success rate
- Failure counts by error category
- Blocked-target counts by policy category

Use tracing spans for:

- Cache lookup
- DNS resolution
- Address validation
- Connect/TLS
- HTTP response
- HTML parsing

Logs should include:

- Request correlation ID
- Redacted or hashed URL
- Hostname
- Final status
- Redirect count
- Duration
- Error category

Do not log query strings, authorization data, cookies, response bodies, or extracted metadata by default.

## 11. Verification plan

### Unit tests

Test:

- URL normalization and cache keys
- Unsupported schemes and credentials
- Every blocked IPv4 and IPv6 range
- IPv4-mapped IPv6 addresses
- Mixed DNS answers containing both allowed and blocked addresses
- Literal IP targets
- Metadata hostname blocking
- Relative, absolute, malformed, looping, and excessive redirects
- Redirects from public addresses to blocked addresses
- Redirects whose hostname resolves differently on each lookup
- Timeout and cancellation propagation
- Response-size limits
- HTML metadata precedence and entity decoding
- Cache expiry, eviction, and concurrent single-flight behavior

### Integration tests

Use a controlled DNS resolver and local test servers to verify:

1. A public test hostname resolves to an allowed address and succeeds.
2. The same hostname later resolves to a private or loopback address and is rejected.
3. A public URL redirects to a link-local or metadata address and is rejected before connection.
4. A redirect chain changes from an allowed address to a blocked address and is rejected.
5. The client dials the validated IP rather than performing an unconstrained second DNS lookup.
6. Proxy environment variables cannot redirect traffic through an unintended proxy.
7. Slow DNS, connection, TLS, headers, and bodies each produce the correct timeout.
8. Oversized responses are terminated and never fully buffered.
9. Concurrent identical requests produce one network fetch and multiple cache results.

The security-critical assertions should verify that the blocked test server receives no connection attempt.
