## Architecture

Expose a small service:

```text
fetchMetadata(url, options) -> {
  finalUrl,
  status,
  contentType,
  title,
  description,
  fetchedAt,
  cache: "hit" | "miss" | "stale"
}
```

Components:

- URL parser and canonicalizer
- SSRF policy checker
- DNS resolver
- HTTP client with a pinned-IP dialer
- redirect handler
- bounded metadata parser
- TTL cache
- metrics, logs, and tracing

## Request Flow

1. Parse and canonicalize the URL.
   - Permit only `http` and `https`.
   - Reject credentials, malformed ports, fragments, and unsupported schemes.
   - Normalize hostname casing, trailing dots, and IDNs.

2. Check the cache using the canonical URL.

3. Resolve the hostname explicitly.

4. Reject the target if any resolved address is unsafe.

5. Connect only to the approved resolved address.
   - Use a custom dialer so the HTTP client cannot perform a second unrestricted DNS lookup.
   - Preserve the original hostname for the `Host` header and TLS SNI.
   - Apply the same policy to every address returned by DNS.

6. Send the request with bounded headers and a strict response body limit.

7. On redirect:
   - Resolve the `Location` against the current URL.
   - Re-run URL validation and DNS resolution.
   - Re-run the IP policy check.
   - Pin the new approved address before connecting.
   - Enforce a small redirect limit, such as five.
   - Do not automatically downgrade HTTPS to HTTP unless explicitly allowed.

8. Parse only the required metadata (`title`, description, canonical URL, Open Graph fields) with a maximum document size and parser timeout.

9. Store the result in the cache and emit observability data.

## SSRF Policy

Reject any resolved IP in:

- IPv4 loopback: `127.0.0.0/8`
- IPv4 private: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`
- IPv4 link-local: `169.254.0.0/16`
- IPv4 unspecified/current network: `0.0.0.0/8`
- IPv4 multicast/reserved ranges
- IPv6 loopback: `::1/128`
- IPv6 unspecified: `::/128`
- IPv6 link-local: `fe80::/10`
- IPv6 unique-local/private: `fc00::/7`
- IPv6 multicast/reserved ranges
- Explicit cloud metadata targets, including:
  - `169.254.169.254`
  - `169.254.170.2`
  - `168.63.129.16`

Also reject hostnames that resolve to any mixture of public and blocked addresses. A hostname is allowed only when every candidate address is approved.

Do not rely on string checks such as “starts with `192.168`”. Parse IP literals structurally, including bracketed IPv6 URLs and IPv4-mapped IPv6 addresses.

Conceptually:

```text
validateTarget(url):
  scheme = validateScheme(url)
  host = normalizeHostname(url.host)

  if host is an IP literal:
      addresses = [parseIP(host)]
  else:
      addresses = dns.resolveAll(host)

  if addresses is empty:
      reject("no DNS answers")

  if any(isBlocked(address) for address in addresses):
      reject("unsafe destination")

  return connectUsingPinnedAddresses(url, addresses)
```

For DNS rebinding resistance, resolve immediately before each connection and bind the socket to the validated address. Never validate one lookup and then let a separate resolver choose the connection address.

## Timeouts and Limits

Use independent limits:

- DNS timeout: 1–2 seconds
- TCP connect timeout: 2–3 seconds
- TLS handshake timeout: 3 seconds
- Response-header timeout: 3–5 seconds
- Total request deadline: 10 seconds
- Maximum redirects: 5
- Maximum response body: 1–2 MB
- Maximum metadata field length
- Maximum concurrent fetches per caller
- Optional rate limit per hostname

Cancel all operations when the request deadline expires.

## Cache

Use a bounded LRU or distributed cache keyed by:

```text
sha256(canonicalUrl + fetchProfileVersion)
```

Store:

- Parsed metadata
- Final URL
- HTTP status
- Fetch timestamp
- Expiration timestamp
- Content hash
- Error classification, where appropriate

Recommended behavior:

- Fresh hit: return immediately.
- Expired entry: fetch synchronously.
- Optional stale-while-revalidate: return stale data while one background refresh runs.
- Request coalescing: only one in-flight fetch per cache key.
- Never cache credentials or full response bodies unless required.

## Observability

Emit structured events and metrics:

- `fetch_started`, `fetch_completed`, `fetch_blocked`
- Cache hit/miss/stale counts
- DNS duration, connect duration, TLS duration, total duration
- Redirect count and final scheme
- Status-code distribution
- Body-size-limit and timeout counts
- SSRF rejection reason and resolved address family

Redact query strings, authorization headers, cookies, and response contents. Include a correlation ID and a stable hostname hash where privacy matters.

## Tests

Test the policy independently with table-driven cases:

- Public IPv4 and IPv6: allowed
- RFC1918, loopback, link-local, multicast, unspecified, and ULA: blocked
- IPv4-mapped IPv6 private address: blocked
- Decimal, hexadecimal, and unusual IPv4 representations: blocked
- Explicit metadata IPs: blocked
- Hostname with both public and private DNS answers: blocked
- DNS failure and empty answers: classified correctly

Integration tests should use a controllable DNS resolver and local HTTP server to verify:

- Initial private target is blocked
- Public URL redirecting to a private IP is blocked
- Public URL redirecting to a hostname resolving privately is blocked
- Redirect chains are revalidated at every hop
- DNS rebinding between validation and connection cannot bypass the policy
- HTTPS SNI and `Host` remain correct when dialing a pinned IP
- Redirect loops and excessive redirects terminate
- DNS, connect, TLS, header, total, and body-size timeouts work
- Cache hits avoid network calls
- Concurrent identical requests are coalesced
- Malformed or oversized HTML is safely handled
- Metrics and redaction behavior are correct

A useful security invariant is: **no socket is opened until the exact destination IP has been resolved, classified as public, and pinned for that connection.**