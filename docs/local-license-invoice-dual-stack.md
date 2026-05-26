# Local dual-stack setup: testing the self-hosted license-invoice flow

This doc describes how to run two GlitchTip backend instances side-by-side on
your laptop so you can end-to-end test the self-hosted license-invoice redirect
(the "view my Stripe invoice" flow that proves a self-hosted user paid for
their GlitchTip license) without pointing your local frontend at production.

## Why two stacks

In production, the architecture splits:

- **Self-hosted instances** (your customers' deployments). `BILLING_ENABLED=False`.
  No Stripe credentials. They store a license key (which is a Stripe customer
  ID) per organization and use it to build a redirect URL pointing at the hosted
  instance.
- **Hosted instance** (`app.glitchtip.com`). `BILLING_ENABLED=True`. Holds the
  Stripe keys. Serves the public `GET /api/0/billing/license-invoice/` endpoint,
  which looks up the customer's most recent Stripe invoice and 302s the user
  to its hosted invoice URL (an "Invoice paid" page with download buttons for
  paid invoices — Stripe's standard hosted invoice view).

The two roles are mutually exclusive on a single backend instance: with Stripe
keys set, the backend reports `billingEnabled: true` and the frontend renders
the hosted UI; without them, the frontend renders the self-hosted UI but the
license-portal endpoint can't reach Stripe.

So to exercise the full flow locally, you need both at once.

## What's in the dual stack

Two backends, each fully isolated (own postgres, own valkey), defined in
`compose.e2e-license.yml` at the repo root.

| Service | Port | BILLING_ENABLED | Stripe keys | Fixtures |
|---|---|---|---|---|
| `selfhosted-web` | `:8000` | `false` | none | `bootstrap_dev` runs |
| `hosted-web` | `:8001` | `true` | from `.env` | none — only serves the public endpoint |
| `mailpit` | `:8025` (web UI) | — | — | captures all emails sent by either backend |

The hosted stack doesn't run `bootstrap_dev` because its only job is to answer
`GET /api/0/billing/license-invoice/?customer_id=cus_xxx`. That endpoint reads
nothing from the hosted-side database; it just calls Stripe and returns a 302.
No users, orgs, or projects need to exist there.

Database and valkey ports are not exposed to the host — those services are
reachable only on the internal compose network, by hostname:

- `selfhosted-postgres`, `selfhosted-valkey`
- `hosted-postgres`, `hosted-valkey`
- `mailpit` (SMTP on :1025, internal only)

## Email capture (Mailpit)

Both backends are configured to send email through a Mailpit container
(`EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend`, `EMAIL_HOST=mailpit`,
`EMAIL_PORT=1025`). Mailpit accepts every SMTP message, stores it locally, and
exposes a web inbox at **http://localhost:8025**.

What you get:
- Every email sent by either stack (license-key recovery, allauth
  email-confirmation, org invites, throttle notices, anything) lands in the
  Mailpit inbox.
- Full HTML rendering with a side-by-side text + raw-source view.
- Click links in the email body to test the recipient experience end-to-end
  (e.g. the email-confirmation link routes back to the frontend).
- Nothing leaves your laptop. No SMTP credentials, no real-inbox risk.

To disable Mailpit and fall back to console-logging emails to stdout:
1. Stop the mailpit service: `docker compose -f compose.e2e-license.yml stop mailpit`
2. Edit the `x-default-env:` block in `compose.e2e-license.yml` and change
   `EMAIL_BACKEND` back to `django.core.mail.backends.console.EmailBackend`.
3. Recreate the webs: `docker compose -f compose.e2e-license.yml up -d`

Emails are persisted in a named volume (`mailpit-data`). To wipe history:
`docker compose -f compose.e2e-license.yml down -v` (removes ALL volumes,
including postgres data — heavier than usually needed). To just clear the
Mailpit inbox without losing data elsewhere, click the trash icon in the
Mailpit web UI.

## File ownership

Three files are involved, and only `compose.e2e-license.yml` and `.env` are new
for this setup:

- `compose.e2e-license.yml` — the dual-stack definition. Lives at repo root so
  Docker can resolve `build: .` correctly. **Not committed.** Add to
  `.git/info/exclude` if you want git to forget about it locally without
  modifying the shared `.gitignore`.
- `.env` — holds `STRIPE_PUBLIC_KEY` and `STRIPE_SECRET_KEY`. Already covered
  by the project's `.gitignore`. Docker Compose auto-loads it.
- `compose.override.yml` — leave alone. It applies to the *normal* single-stack
  `docker compose up`, not the dual stack (which is invoked with `-f
  compose.e2e-license.yml` and ignores `compose.override.yml` unless you
  explicitly pass both `-f` flags).

## Prerequisites

You need Stripe test-mode API keys from a sandbox you control. Put them in
`.env` at the repo root:

```env
STRIPE_PUBLIC_KEY=pk_test_...
STRIPE_SECRET_KEY=sk_test_...
```

Test-mode keys can't move real money, but they're still credentials — treat
them as secrets. Rotate them in the Stripe dashboard when you're done testing.

## Bringing it up

```sh
# Stop the normal single-stack first (port 8000 conflicts otherwise)
docker compose down

# Start the dual stack
docker compose -f compose.e2e-license.yml up -d

# Wait ~30 seconds for both backends to migrate and boot, then verify:
curl -s http://localhost:8000/api/settings/ | jq '.billingEnabled, .version'
# → false, "dev-selfhosted"

curl -s http://localhost:8001/api/settings/ | jq '.billingEnabled, .version'
# → true, "dev-hosted"
```

The version field is set differently per stack (`dev-selfhosted` vs `dev-hosted`)
so you can tell at a glance which one you're hitting.

## Frontend wiring

The frontend currently hardcodes the production hosted URL in
[`license-certificate.component.ts`][lc]:

```ts
const LICENSE_PORTAL_URL =
  "https://app.glitchtip.com/api/0/billing/license-invoice/";
```

For local dual-stack testing, change this to `http://localhost:8001/...`.
Don't ship that change — production must point at the real hosted URL.

The longer-term fix is to read this URL from a settings field served by the
self-hosted backend (e.g. a new `hostedUrl` key on `/api/settings/`), so each
self-hosted deployment can be told where its hosted counterpart lives. That
work is out of scope here.

[lc]: ../glitchtip-frontend/src/app/settings/subscription/license-certificate/license-certificate.component.ts

Your Angular dev server points at `localhost:8000` (the self-hosted backend)
exactly as it does today. Only the one URL constant changes.

## Creating a test customer

The hosted backend talks to your Stripe sandbox, so you need a customer ID
that actually exists there:

```sh
source .env
curl -s https://api.stripe.com/v1/customers \
  -u "$STRIPE_SECRET_KEY:" \
  -d "description=local license-portal test" \
  -d "email=licensetest@example.com" \
  | jq -r .id
# → cus_xxxxxxxxxxxxxxxx
```

Then save that ID as the `licenseKey` on the self-hosted org:

```sh
curl -s -X PUT \
  -H "Authorization: Bearer dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd" \
  -H "Content-Type: application/json" \
  -d '{"licenseKey": "cus_xxxxxxxxxxxxxxxx"}' \
  http://localhost:8000/api/0/organizations/org/
```

(The bootstrap token is the same 64-`d` token used by the normal dev stack.)

For the customer to have an invoice to view, you'll also need to create and
finalize one in Stripe. The simplest test-mode shortcut:

```sh
source .env
CUSTOMER_ID=cus_xxxxxxxxxxxxxxxx   # from above

# Invoice item ($1)
curl -s https://api.stripe.com/v1/invoiceitems -u "$STRIPE_SECRET_KEY:" \
  -d "customer=$CUSTOMER_ID" -d "amount=100" -d "currency=usd" \
  -d "description=GlitchTip license"

# Create + finalize + mark paid
INVOICE_ID=$(curl -s https://api.stripe.com/v1/invoices -u "$STRIPE_SECRET_KEY:" \
  -d "customer=$CUSTOMER_ID" -d "collection_method=send_invoice" \
  -d "days_until_due=30" | jq -r .id)
curl -s -X POST https://api.stripe.com/v1/invoices/$INVOICE_ID/finalize \
  -u "$STRIPE_SECRET_KEY:" > /dev/null
curl -s -X POST https://api.stripe.com/v1/invoices/$INVOICE_ID/pay \
  -u "$STRIPE_SECRET_KEY:" -d "paid_out_of_band=true" > /dev/null
```

Now in the self-hosted UI, the license-key field will show that customer ID,
and clicking the "View License" / proof-of-purchase link will:

1. Hit `http://localhost:8001/api/0/billing/license-invoice/?customer_id=cus_xxx`
2. Hosted backend calls Stripe, fetches the customer's most recent invoice
3. 302 redirects the user to `https://invoice.stripe.com/i/...`
4. User lands on the Stripe-hosted invoice page — "Invoice paid", amount, date,
   "Download invoice" and "Download receipt" buttons

If the customer has no invoices yet (or doesn't exist in Stripe), the redirect
goes to the configured fallback URL (`STRIPE_PORTAL_LOGIN_URL`, default
`https://billing.stripe.com/p/login/28E4gA8Eb8ZE6jib97ds400`) so the user can
log in by email and find their billing info there.

## What this setup actually tests

| Behavior | Covered |
|---|---|
| Self-hosted backend correctly reports `billingEnabled: false` | ✅ |
| Self-hosted frontend renders self-hosted UI | ✅ |
| Frontend reads `licenseKey` from the org and builds the invoice URL | ✅ |
| Cross-host redirect (`:8000` → `:8001`) actually works | ✅ |
| CORS / cookie scoping behaves correctly across the two hosts | ✅ |
| Hosted endpoint correctly authenticates to Stripe | ✅ |
| Stripe returns a real hosted invoice URL for a customer with invoices | ✅ |
| Customer with no invoices falls back to `billing.stripe.com/p/login/...` | ✅ |
| Unknown customer ID falls back to `billing.stripe.com/p/login/...` | ✅ |
| Malformed customer ID returns 400 | ✅ |
| Per-IP rate limit on the hosted endpoint (10/min) | ✅ |

What it doesn't test:

- TLS / HSTS behavior (both stacks are HTTP locally)
- The real production fallback URL slug — your `.env` has the test-mode keys
  pointing at the same Stripe account that owns the prod portal config, so
  this is realistic; but if your local Stripe sandbox is a different account,
  the fallback URL would need updating via `STRIPE_PORTAL_LOGIN_URL` on the
  hosted stack
- Multi-region Stripe behavior (we don't set `STRIPE_REGION`)

## Tearing down

```sh
# Stop the dual stack
docker compose -f compose.e2e-license.yml down

# (Optional) Remove the test customer from Stripe
source .env
curl -s -X DELETE \
  https://api.stripe.com/v1/customers/cus_xxxxxxxxxxxxxxxx \
  -u "$STRIPE_SECRET_KEY:"

# Return to the normal single-stack
docker compose up -d
```

To completely wipe the dual stack's data (start fresh):

```sh
docker compose -f compose.e2e-license.yml down -v
```

The `-v` removes named volumes, so both postgres databases get reset on next
boot. `bootstrap_dev` will re-run on the self-hosted side and recreate the
default user/org/project.

## Gotchas

- **Port 8000 conflict.** If `docker compose up` (the normal single-stack) is
  running, `docker compose -f compose.e2e-license.yml up` will fail because
  both want port 8000. Take the normal stack down first.
- **Two boot times.** On a cold start, each backend runs its own migrations
  and `bootstrap_dev` (on the self-hosted side). Expect ~30-60 seconds
  total before both are answering.
- **Browser cookies are per-host, not per-port.** `localhost:8000` and
  `localhost:8001` share the same `localhost` cookie scope. If you log in on
  one and then visit the other, you may carry session state across — this is
  fine for the license-invoice flow (the endpoint is `auth=None`) but can
  surprise you if you start poking at authenticated routes on `:8001`.

- **Hosted invoice URLs expire.** Stripe documents that a hosted invoice URL
  expires 30 days after the invoice's due date (or 30 days after finalization
  if no due date), max 120 days. The redirect endpoint mints a fresh URL on
  every click, so this is only a problem if the customer's *most recent
  invoice* is older than the expiry window — which shouldn't happen for active
  subscription customers but could for one-off purchases.
- **Stripe rate limits.** The dual stack hits real Stripe APIs. Stripe's
  test-mode rate limits are generous but not infinite; if you run automated
  tests in a loop, you can hit them.
- **The keys you paste end up in the Stripe dashboard.** Rotate when done.
