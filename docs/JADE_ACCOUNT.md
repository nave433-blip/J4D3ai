# Cortana Account — protocol sketch (future hosted service)

> **Status: design document, not a shipped service.** The hosted Cortana
> cloud backend described here does **not** exist yet. This round delivers
> the client-side slot only:
>
> - `core/cortana_account.py` — the local account record
>   (`~/.cortana/account.json`)
> - `core/signin.py` — Sign in with Microsoft / Apple / Google / GitHub via
>   standard OAuth2/PKCE; tokens stay in the OS keyring
>
> Nothing below should be read as a claim that a Cortana cloud is live.

## Purpose

A Cortana Account gives the user one identity across devices and gives a
future hosted service a way to authenticate the CLI/desktop client, sync
*user-opted-in* data (never keys, never private vector memories without
explicit consent), and attach entitlements (e.g. hosted models).

## Client record (shipped)

`~/.cortana/account.json`:

```json
{
  "display_name": "Ada",
  "email": "ada@example.com",
  "linked": {
    "github": {"account": "ada-dev", "linked_at": 1790000000.0}
  },
  "created": 1790000000.0,
  "format": "cortana-account/1"
}
```

## Proposed protocol (future)

All calls over TLS 1.2+, bearer tokens never logged, PII minimized.

- `POST /v1/accounts` — create account from a verified third-party
  identity token (Sign in with Apple / Google / Microsoft / GitHub).
  Returns a Cortana refresh token (keyring-stored, rotatable).
- `POST /v1/token/refresh` — rotate access tokens.
- `GET /v1/me` — profile + entitlements.
- `POST /v1/sync/push` / `GET /v1/sync/pull` — sync *only* the data classes
  the user explicitly enables (settings, prompt library, sims). Explicitly
  out of scope: provider API keys, OAuth tokens, private vector memories —
  these never leave the device.
- `DELETE /v1/me` — full server-side erasure (GDPR-style).

## Drop-in contract for whoever builds the server

1. The CLI already collects everything the server needs: verified
   third-party identity tokens + a local account record.
2. Server issues Cortana-scoped tokens; the client stores them exactly like
   today's third-party tokens (keyring service `cortana_signin_cortana`).
3. The client feature-detects the server: `GET /v1/status` → if unreachable,
   every account feature degrades to the local record with a clear message.
   Offline-first is a requirement, not a fallback.

## What Microsoft would need to provide

- An OAuth client (or first-party blessing) for "Sign in with Microsoft"
  against the Cortana app id.
- The hosted API above (or a mapping onto existing Microsoft identity
  infrastructure).
- A data-processing addendum covering the sync data classes.

Until then: local record, local sign-ins, no server — and honest about it.
