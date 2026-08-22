## Architecture

Expose a single operation:

```text
fetchMetadata(rawURL) -> {
  finalUrl,
  title?,
  description?,
  contentType?,
  statusCode,
  fetchedAt,
  cacheStatus
}
```

Use a dedicated outbound HTTP client with system proxies disabled. Do not accept caller-supplied headers, credentials, proxy settings, or redirect behavior.

Request flow:

```text
Normalize URL
  -> Check cache
  -> Resolve hostname
  -> Validate every resolved IP
  -> Connect to one validated IP while preserving the hostname for Host/SNI
  -> Read bounded response
  -> Validate each redirect from scratch
  -> Parse metadata
  -> Cache result
```

## URL Policy

Accept only absolute `http` and `https` URLs.

Reject:

- Embedded credentials
- Missing host
- Noncanonical or malformed IP literals
- IPv6 zone identifiers
- Control characters
- URLs exceeding a configured length
- Ports outside an explicit policy, preferably only `80` and `443`

Canonicalize for cache keys by lowercasing scheme/host, converting international hostnames to ASCII, removing fragments, removing default ports, and normalizing the path without changing query semantics.

## SSRF Protection

For every initial request and redirect:

1. Parse and canonicalize the destination.
2. If the host is an IP literal, validate it directly.
3. Otherwise resolve all `A` and `AAAA` records using a trusted resolver.
4. Reject the destination if any answer is non-public.
5. Select one validated address.
6. Connect directly to that exact address.
7. Preserve the original hostname for HTTP `Host` and TLS SNI/certificate verification.

Pinning the connection to the validated address closes the DNS rebinding gap between validation and connection. Do not let the HTTP library resolve the hostname again.

Block all non-global destinations, including:

- IPv4 and IPv6 loopback
- RFC1918 private space
- IPv4 and IPv6 link-local
- Unique-local IPv6
- Unspecified addresses
- Multicast
- Broadcast and reserved ranges
- Carrier-grade NAT space
- Documentation and benchmarking ranges
- IPv4-mapped IPv6 representations of blocked IPv4 addresses
- Cloud metadata endpoints, including `169.254.169.254`, their IPv6 equivalents, and provider metadata hostnames

Use a maintained IP classification library plus an explicit denylist. Avoid string-prefix IP checks.

Redirects must be processed manually. Disable automatic redirects and repeat the full parse, DNS resolution, IP validation, and pinned connection procedure for every `Location`. Reject malformed, non-HTTP(S), or policy-violating redirects. Limit redirect count, for example to five, and detect loops.

Deployment should add a second control: outbound firewall rules allowing only public internet destinations. Application validation remains necessary because network policy alone may vary by environment.

## Resource Limits

Apply separate deadlines:

- DNS: 1 second
- Connect: 2 seconds
- TLS handshake: 2 seconds
- First byte: 3 seconds
- Idle read: 2 seconds
- Total operation, including redirects: 8 seconds

Also enforce:

- Maximum five redirects
- Maximum compressed response size
- Maximum decompressed response size, for example 2 MiB
- Maximum HTML parsing input
- Restricted accepted content types
- Limited connection pool and per-host concurrency
- Cancellation propagation when the total deadline expires

Read through a bounded stream. Do not rely only on `Content-Length`, since it may be absent or false. Treat decompression expansion as part of the response-size limit.

A `HEAD` request is not sufficient because many sites omit metadata or handle it differently. Use a bounded `GET`, optionally requesting an initial byte range, while tolerating servers that ignore range requests.

## Caching

Cache only successful, policy-compliant results.

A cache entry should contain:

```text
canonical URL
metadata
final canonical URL
fetch timestamp
expiry
ETag and Last-Modified, when present
```

Recommended behavior:

- TTL based on trusted response headers but capped by service policy
- Conservative default TTL, such as 15 minutes
- Conditional refresh with `If-None-Match` or `If-Modified-Since`
- Request coalescing so concurrent misses for the same URL perform one fetch
- Short negative caching for ordinary public failures
- Never cache policy denials as proof that a hostname remains unsafe or safe
- Re-run destination validation before every network revalidation

Keep cache keys independent of DNS results. Do not serve cached metadata across authorization or tenant boundaries if output can vary by tenant configuration.

## Metadata Parsing

Parse HTML with a real HTML parser. Extract a bounded set of fields, such as:

- `<title>`
- Open Graph title and description
- Standard meta description
- Canonical URL, treated only as metadata and never fetched automatically

Define deterministic precedence and trim field lengths. Decode only supported character encodings and return partial metadata when parsing fails after a valid bounded response.

Do not execute JavaScript, load images, resolve embedded resources, or follow HTML refresh directives.

## Observability

Emit one structured event per attempt and redirect hop:

```text
request_id
cache_status
normalized_scheme
destination_host_hash
selected_ip_class
redirect_count
status_code
content_type
bytes_read
dns_ms
connect_ms
tls_ms
first_byte_ms
total_ms
outcome
policy_denial_reason
```

Do not log full URLs by default because paths, queries, and credentials may contain secrets. Log a hostname hash or approved hostname plus a redacted URL form.

Metrics should include:

- Request count and latency
- Cache hit, miss, and revalidation rates
- Outcomes by category
- SSRF policy denials by reason
- DNS, connect, TLS, and read timeouts
- Redirect depth
- Downloaded and decompressed byte counts
- Coalesced request count
- Parser failures

Create tracing spans for cache lookup, each DNS lookup, each redirect hop, connection, response read, and parsing. Alerts should cover spikes in policy denials, timeout rates, redirect loops, and outbound attempts rejected by the network layer.

## Tests

Use a controlled DNS resolver and local HTTP/TLS test servers. Never make tests depend on the public internet.

Core test cases:

- Public IPv4 and IPv6 targets succeed.
- Loopback, private, link-local, unique-local, multicast, unspecified, reserved, and metadata IPs fail.
- Decimal, octal-like, hexadecimal, shortened, mixed, and IPv4-mapped IPv6 representations cannot bypass validation.
- A hostname resolving to both public and private addresses is rejected.
- DNS rebinding cannot change the actual connection destination after validation.
- Redirect from public to private, loopback, link-local, or metadata address fails before connection.
- Every hop is resolved and validated independently.
- Relative redirects work; redirect loops and excess hops fail.
- TLS uses the original hostname for SNI and certificate validation while connecting to the pinned IP.
- Environment proxy variables do not redirect traffic through a proxy.
- DNS, connect, TLS, first-byte, idle-read, and total deadlines fire independently.
- Infinite streams, false `Content-Length`, chunked responses, and decompression bombs hit size limits.
- Unsupported schemes and embedded credentials fail.
- Cache hits avoid network calls.
- Concurrent misses are coalesced.
- Expired entries revalidate correctly after fresh SSRF checks.
- Failed or blocked fetches do not poison successful entries.
- Sensitive query values and credentials never appear in logs.
- Cancellation closes response bodies and releases pool capacity.

Add property-based tests for URL parsing and IP normalization, and fuzz the URL parser, redirect handling, content encoding, and HTML parser. The key invariant is: no socket may be opened until the exact destination IP for that socket has passed the public-address policy.