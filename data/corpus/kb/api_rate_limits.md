# API and Rate Limits

The Acme Cloud REST API is available on Pro, Business and Enterprise plans. Authenticate with OAuth 2.0 or a personal access token.

## Rate limits
- Pro: 600 requests per minute per workspace.
- Business: 3,000 requests per minute per workspace.
- Enterprise: 10,000 requests per minute per workspace, adjustable on request.

When the limit is exceeded, the API returns HTTP 429 with a Retry-After header in seconds. Clients should back off exponentially with jitter.

## Upload limits
Uploads larger than 150 MB must use the resumable upload session endpoint, which accepts chunks of 8 MB to 64 MB. Upload sessions expire after 48 hours.

## Webhooks
Webhooks are delivered with an HMAC-SHA256 signature in the X-Acme-Signature header. Failed deliveries are retried with exponential backoff for up to 24 hours.
