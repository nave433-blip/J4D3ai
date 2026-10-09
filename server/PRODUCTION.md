# PRODUCTION.md — hardening the reference server

This server is a **reference implementation**. It is correct-by-construction
for the happy path and honest about its limits, but it is NOT hardened for
real users. Do every item below before exposing it publicly. Items are
ordered by risk.

## 1. Database — swap SQLite for Postgres ★ critical

`server/db.py` exposes a narrow `Database` interface (parameterized queries
only). Implement it against Postgres (`psycopg[binary]`):

- one connection pool, `FOR UPDATE` on session/pairing reads that mutate
- the docker-compose `db` service is already provisioned; set `DATABASE_URL`
  and remove the sqlite fallback error in `ServerConfig.validate()`
- run schema migrations with a real tool (Alembic); the `SCHEMA` string is
  a starting point, not a migration story

## 2. Secrets — move out of env files ★ critical

- `JADE_TOKEN_KEY`, OAuth client secrets, `chat_api_key`: Azure Key
  Vault (or equivalent) with managed-identity access; the bicep template
  already uses Container Apps secrets — promote them to Key Vault
  references
- rotate `JADE_TOKEN_KEY` on a schedule; build a re-encryption job
  (decrypt with old key, encrypt with new) before you need it
- never log secrets: the app redacts config and never logs `Authorization`
  headers — keep that invariant in every change (add a test)

## 3. Password hashing — Argon2id ★ high

`server/accounts.py` uses PBKDF2-SHA256 (600k iterations) because it is
stdlib. Swap `hash_password`/`verify_password` for `argon2-cffi`
(Argon2id, OWASP parameters). The stored `algo$…` format carries an
algorithm tag — write a migration that re-hashes on next login.

## 4. TLS + edge ★ critical

- terminate TLS at the edge (Container Apps ingress, Application Gateway,
  or Cloudflare); never serve this over plain HTTP publicly
- set HSTS; the app already sends `nosniff` / `DENY` / `no-referrer`

## 5. Rate limiting + abuse ★ high

The in-memory `RateLimiter` does not survive restarts or scale-out. Move
to Redis (the compose `cache` service is provisioned): sliding-window per
IP *and* per account, stricter buckets for `/login`, `/signup`,
`/pairing/claim`, `/v1/auth/*`. Add CAPTCHA or proof-of-work on signup.

## 6. Auth lifecycle ★ high

- email verification (mailer + token flow) and password-reset flow — both
  deliberately absent here
- refresh-token rotation for sessions (currently long-lived bearer
  tokens); add device management (`GET /v1/accounts/sessions`, revoke)
- OIDC: pin provider JWKS with caching + background refresh; alert on
  key rotation failures
- GitHub login: verify `code` single-use; consider requiring 2FA-enrolled
  accounts for sensitive connector scopes

## 7. Audit logging ★ high

Log (structured, to the analytics workspace): logins, signups, password
changes, connector connect/disconnect, pairing accept/reject, token
refresh failures. Never log tokens, codes, or secrets. Retain per policy.

## 8. Backups + DR ★ high

- nightly encrypted Postgres backups with tested restores
- the Fernet `JADE_TOKEN_KEY` is **not** recoverable from a backup of
  the DB alone — back it up separately (Key Vault) or connector tokens
  become permanently unreadable

## 9. Chat endpoint ★ medium

- per-user/per-key quotas and cost ceilings on `/v1/chat/completions`
- content-safety policy for the hosted path (the on-device path is the
  user's own machine; the hosted path is *your* liability)
- decide the data-retention policy for relayed prompts; default to zero
  retention and say so

## 10. Compliance ★ medium

SOC 2 / ISO 27001 readiness: the checklist above plus access reviews,
incident runbooks, dependency scanning (`pip-audit` in CI), and a
published security contact. This repo ships no compliance artifacts.

## Deliberately out of scope (not planned)

- Multi-tenancy / organizations — the data model is single-user-account
  oriented; adding orgs is a redesign, not a patch
- Email/SMS delivery — no mailer is bundled on purpose
