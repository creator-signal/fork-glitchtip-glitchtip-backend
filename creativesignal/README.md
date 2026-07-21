# Creator Signal ZITADEL integration

This directory contains the Creator Signal-specific layer for the GlitchTip
backend fork. GlitchTip's generic django-allauth OIDC implementation remains
unchanged; this layer configures it, closes password authentication and
reconciles the governed Creator Signal resources.

## Runtime contract

Set the following environment variables on every GlitchTip web, worker and
reconciliation container:

```text
CREATOR_SIGNAL_SSO_ONLY=true
ENABLE_USER_REGISTRATION=false
ENABLE_SOCIAL_APPS_USER_REGISTRATION=true
ENABLE_ORGANIZATION_CREATION=false
```

Mount three non-empty files in
`GLITCHTIP_ZITADEL_CREDENTIAL_DIRECTORY` (default `/run/zitadel`):

- `client-id`
- `client-secret`
- `operator-email`

Set `ZITADEL_DISCOVERY_URL` to the complete discovery URL and mount a writable,
private `GLITCHTIP_BOOTSTRAP_DIRECTORY` (default `/run/bootstrap`) for generated
DSNs and `status.json`. Then run after migrations:

```sh
python -m creativesignal.configure_zitadel
```

The command is idempotent. It creates or updates the `zitadel` OIDC provider,
the Creator Signal organisation, provider association, operator team, initial
operator and four governed projects. A changed client secret is updated without
creating another provider. Credential values are never included in output.

ZITADEL must restrict the application to assigned project roles. The callback
URLs are:

```text
http://localhost:48220/accounts/oidc/zitadel/login/callback/
https://errors.creatorsignal.me/accounts/oidc/zitadel/login/callback/
```

The Django admin login remains available for an independently managed
break-glass superuser. Routine GlitchTip login, account creation and recovery
are ZITADEL-only.
