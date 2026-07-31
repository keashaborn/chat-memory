# AI Operations alert delivery v1

## Scope

This candidate adds a private, metadata-only email outbox and bounded Resend
worker for server-owned AI Operations incidents. It does not change the
authoritative incident inbox, user chat, trusted-web routing, Supabase identity,
or the Verbal Sage browser runtime.

## Audit baseline

- Verbal Sage already uses a server-only Resend request for access-request
  notifications. `RESEND_API_KEY` and `ACCESS_REQUEST_NOTIFY_EMAIL` are present
  in root-owned mode `0600` `/etc/verbalsage-v2.env`.
- seebx trusted-web monitoring already supports an optional metadata-only HTTPS
  webhook, but `TRUSTED_WEB_MONITOR_ALERT_WEBHOOK_URL` is empty in production.
- The private AI Operations inbox is authoritative and is already populated by
  the hourly trusted-web monitor.

No secret value, recipient address, prompt, query, URL, retrieved content, or
answer text was read into this document or stored in the candidate.

## Authority boundary

1. The existing database monitor writer inserts an append-only incident event.
2. A database trigger enqueues one email delivery only for the first normal
   critical incident event. Warning incidents remain visible in the private AI
   Operations inbox but do not send immediate email. Repeated observations
   cannot enqueue a duplicate delivery.
3. `brains_app` has no direct table access. A resource-limited worker can claim
   and complete deliveries only through security-definer functions and two
   transaction-local authorization settings.
4. The recipient and Resend credential remain root-owned environment settings
   and are never stored in Postgres.
5. The existing Supabase fresh-user and capability checks remain authoritative
   for viewing, acknowledging, and resolving incidents in the admin console.

## Failure behavior

- Provider timeouts, transport failures, HTTP 429, and HTTP 5xx are retryable.
- Other HTTP 4xx responses are permanent failures.
- Retry delays are 1 minute, 5 minutes, 30 minutes, 2 hours, and 12 hours.
- A crashed worker lease is recoverable after 10 minutes. A delivery that has
  exhausted its attempt budget becomes permanently failed.
- The provider idempotency key is derived from the delivery UUID, so a retry
  cannot create a new logical delivery.
- Delivery failures never delete, resolve, or modify the authoritative monitor
  incident.

## Deployment gates

Production deployment requires separate approval for:

1. applying the migration and retaining its rollback;
2. installing the service, timer, and root-owned mode `0600` environment file;
3. securely provisioning a Resend key and recipient on seebx;
4. running the authorized metadata-only delivery drill;
5. verifying one provider delivery, one audit trail, zero user content, and
   healthy service/timer state.
