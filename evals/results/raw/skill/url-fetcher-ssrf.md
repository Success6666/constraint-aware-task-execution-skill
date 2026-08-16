Design the fetcher as a small service with one security-critical request pipeline. Do not use an HTTP client’s automatic redirect handling.

**Request Flow**

```text
Input URL
   |
Parse and normalize
   |
Resolve hostname
   |
Validate every resolved IP
   |
Connect to one validated IP while preserving hostname for TLS/Host
   |
Fetch response headers and bounded body
   |
Redirect? Repeat from parsing and DNS resolution
   |
Extract metadata
   |
Cache result
```

### API

```ts
type Metadata = {
  canonicalUrl: string;
  title?: string;
  description?: string;
  imageUrl?: string;
  contentType?: string;
  siteName?: string;
  fetchedAt: string;
};

type FetchOptions = {
  totalTimeoutMs?: number;       // default 5000
  connectTimeoutMs?: number;     // default 1500
  responseTimeoutMs?: number;    // default 3000
  maxRedirects?: number;         // default 5
  maxBodyBytes?: number;         // default 1 MiB
  cacheTtlSeconds?: number;      // default 3600
};

fetchMetadata(url: string, options?: FetchOptions): Promise<Metadata>;
```

## SSRF Protection

Accept only absolute `http:` and `https:` URLs. Reject credentials, malformed ports, invalid hostnames, and URLs exceeding a reasonable length.

For every request and redirect:

1. Parse the URL with a standard URL parser.
2. Resolve all `A` and `AAAA` records.
3. Reject the target if resolution fails or any returned address is prohibited.
4. Select a validated address.
5. Pin the TCP connection to that exact address.
6. Preserve the original hostname for the HTTP `Host` header and TLS SNI.
7. Disable automatic redirects.
8. Resolve and validate the redirect target independently.

Reject at least:

- IPv4 and IPv6 loopback
- RFC1918 private IPv4 ranges
- IPv4 and IPv6 link-local ranges
- IPv6 unique-local addresses
- Unspecified addresses
- Multicast and reserved/special-purpose ranges
- Carrier-grade NAT ranges
- IPv4-mapped IPv6 representations of prohibited IPv4 addresses
- Cloud metadata destinations, including `169.254.169.254`, metadata hostnames, and provider-specific aliases

Use a maintained IP-address library or standard platform classification routines. Avoid string-prefix checks.

The connection must use the validated IP rather than resolving the hostname again inside the HTTP client. Otherwise, DNS rebinding can bypass validation. If the client cannot pin an address while preserving SNI, use a custom dialer or transport.

Redirect resolution must follow normal URL semantics:

```ts
const nextUrl = new URL(locationHeader, currentUrl);
```

Each hop consumes the same total deadline and redirect budget. Reject redirects to unsupported schemes or missing/invalid `Location` values.

## Fetching and Parsing

Prefer `GET` because many sites handle `HEAD` incorrectly. Request HTML with a clear user agent and compressed-response support.

Enforce limits while streaming:

- Reject bodies larger than `maxBodyBytes`, including after decompression.
- Stop reading once the document `<head>` has been parsed or the limit is reached.
- Accept HTML-compatible content types only.
- Do not execute JavaScript.
- Do not fetch linked images, icons, stylesheets, or scripts.
- Decode text using a bounded, supported charset strategy.

Metadata precedence can be:

```text
Title:       og:title → twitter:title → <title>
Description: og:description → twitter:description → meta[name=description]
Image:       og:image → twitter:image
Site name:   og:site_name
Canonical:   <link rel=canonical> → final response URL
```

Resolve relative canonical and image URLs against the final response URL. Return these URLs without fetching them.

## Caching

Use normalized input URL plus metadata-affecting options as the cache key. Preserve path and query because they may identify different content; remove fragments because they are not sent to servers.

Store:

```ts
type CacheEntry = {
  value?: Metadata;
  errorKind?: "not_found" | "unsupported_content";
  expiresAt: number;
};
```

Recommended behavior:

- Successful results: cache for one hour, bounded by configuration.
- Stable negative results such as `404`: cache briefly, around five minutes.
- Do not cache timeouts, DNS failures, connection failures, SSRF rejections, or `5xx` responses.
- Coalesce concurrent misses for the same key into one in-flight request.
- Apply a maximum entry count and eviction policy such as LRU.
- Never allow a cached result to bypass validation when a network refresh occurs.

For distributed deployments, use a shared cache only if cross-instance consistency is useful. In-flight coalescing can remain local.

## Timeouts and Resource Limits

Use a single absolute deadline for the full operation, with shorter phase limits:

```text
Total request:       5 seconds
DNS:                 1 second
TCP/TLS connection:  1.5 seconds
First response byte: 3 seconds
Body streaming:      remaining total deadline
Redirects:           5
Response bytes:      1 MiB after decompression
```

Cancellation must close DNS, socket, TLS, and body-read operations. Limit global and per-host concurrency to prevent slow destinations from exhausting connections.

## Observability

Emit one structured completion event per fetch:

```json
{
  "operation": "url_metadata_fetch",
  "result": "success",
  "cache": "miss",
  "duration_ms": 184,
  "dns_ms": 12,
  "connect_ms": 31,
  "redirect_count": 1,
  "response_status": 200,
  "response_bytes": 42871,
  "error_kind": null
}
```

Useful metrics:

- Request count by result and cache status
- Latency histogram
- DNS, connection, TLS, and first-byte latency
- Redirect count
- Downloaded bytes
- Timeout and cancellation count
- Rejections by reason
- In-flight requests and coalesced requests

Do not log full URLs by default because queries can contain secrets. Log the scheme, sanitized hostname, port, and a keyed hash of the normalized URL. Do not expose resolved private addresses or response bodies in user-facing errors.

Use stable error categories such as:

```text
invalid_url
unsupported_scheme
blocked_destination
dns_failure
connect_timeout
request_timeout
redirect_limit
response_too_large
unsupported_content
parse_failure
upstream_status
```

## Test Strategy

Unit tests should cover:

- URL parsing, normalization, fragments, credentials, ports, and internationalized names
- Every blocked IPv4 and IPv6 category, including boundary addresses
- IPv4-mapped IPv6 addresses
- Mixed DNS results containing both public and prohibited addresses
- Relative, absolute, protocol-relative, and malformed redirects
- Metadata precedence and relative URL resolution
- Body-size and decompression limits
- Cache expiry, negative caching, eviction, and request coalescing
- Deadline propagation and error classification

Integration tests should use injected DNS and a controllable HTTP transport. Avoid relying on public DNS or external websites.

Critical security cases include:

1. A public hostname resolves directly to `127.0.0.1`.
2. A public hostname resolves to `169.254.169.254`.
3. The first response redirects to a private address.
4. A redirect hostname resolves to both public and private addresses.
5. DNS returns a public address during validation but a private address later; verify that the connection still uses the originally validated address.
6. An IPv6 literal targets loopback, link-local, or unique-local space.
7. A decimal, hexadecimal, shortened, or mapped representation resolves to a prohibited address.
8. Redirect chains exceed the configured limit.
9. A slow DNS lookup, connection, response, and body stream each respect cancellation.
10. A compressed response expands beyond the body limit.

The central invariant should be expressed directly in transport tests: every outbound socket’s destination IP must be one of the addresses resolved and approved for that specific redirect hop.