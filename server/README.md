# Jade reference cloud server

The honest minimal backend a local-first assistant actually needs. The CLI
stays fully functional offline — the server adds **accounts**, **OAuth
brokerage** (one registered OAuth app per provider instead of one per
user), **server-assisted device pairing** (signaling only), and a hosted
**OpenAI-compatible chat endpoint**.

Reference implementation, **not production-hardened** — read
[server/PRODUCTION.md](PRODUCTION.md) before real users touch it.

## 5-minute deploy

```bash
cd jade            # repo root
cp .env.example .env  # fill in secrets (JADE_TOKEN_KEY at minimum)
docker compose -f server/docker-compose.yml up --build
```

Then:

```bash
curl localhost:8080/healthz
# {"ok": true, "version": "0.1.0"}
```

Create your first account:

```bash
curl -X POST localhost:8080/v1/accounts/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"a-very-long-passphrase","display_name":"You"}'

curl -X POST localhost:8080/v1/accounts/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"a-very-long-passphrase"}'
# -> {"user": {...}, "session_token": "...", "token_type": "Bearer"}
```

Use the token as `Authorization: Bearer <session_token>` on everything below.

No Docker? The server is stdlib + `cryptography`:

```bash
pip install -r server/requirements.txt
JADE_DATA_DIR=./server-data JADE_TOKEN_KEY="$(openssl rand -base64 32)" \
  python -m server.app
```

## Azure (for Microsoft)

Build, push, deploy to Container Apps:

```bash
az acr create -g jade-rg -n jadereg --sku Basic
az acr build -g jade-rg -r jadereg -t jade-server:latest -f server/Dockerfile .
az group create -n jade-rg -l eastus
az deployment group create -g jade-rg -f azure/container-apps.bicep \
  -p containerImage=jadereg.azurecr.io/jade-server:latest \
     tokenKey="$(openssl rand -base64 32)" \
     publicUrl="https://<fqdn-from-output>"
```

(The bicep file was authored carefully but `az bicep build` validation is
the operator's step — see PRODUCTION.md.)

## API reference

Base: `$JADE_PUBLIC_URL` (default `http://localhost:8080`).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | – | liveness |
| POST | `/v1/accounts/signup` | – | `{email, password≥12, display_name}` |
| POST | `/v1/accounts/login` | – | `{email, password, device_label}` → session token |
| POST | `/v1/accounts/logout` | ✓ | revoke this session |
| GET | `/v1/accounts/me` | ✓ | current user |
| POST | `/v1/accounts/change-password` | ✓ | `{current_password, new_password}` |
| POST | `/v1/auth/oidc/{google,apple,microsoft}` | – | `{id_token, device_label}` → session (ID token verified server-side: RS256 signature, iss/aud/exp) |
| POST | `/v1/auth/github` | – | `{code, device_label}` → session (server exchanges code, reads `GET /user`) |
| GET | `/v1/connectors` | ✓ | catalog + connected status |
| GET | `/v1/connectors/{id}/authorize` | ✓ | 302 to provider (PKCE; server holds the OAuth app) |
| GET | `/v1/connectors/{id}/callback` | – | provider redirect target; stores encrypted tokens |
| POST | `/v1/connectors/{id}/token` | ✓ | fresh access token (refresh handled server-side; clients never see refresh tokens) |
| DELETE | `/v1/connectors/{id}` | ✓ | disconnect |
| POST | `/v1/pairing/create` | ✓ | `{device_name}` → `{pairing_id, code}` (10-min TTL) |
| POST | `/v1/pairing/claim` | – | `{code, device_name}` → `{pairing_id, claim_receipt}` |
| GET | `/v1/pairing/{id}/status` | ✓/receipt | owner: session; claimer: `?receipt=` |
| POST | `/v1/pairing/{id}/accept` | ✓ | `{endpoint_hint}` — owner explicitly accepts |
| POST | `/v1/pairing/{id}/reject` | ✓ | owner declines |
| POST | `/v1/chat/completions` | ✓ | OpenAI-compatible; `{model, messages, stream?}` |

Connector ids: `google-drive`, `gmail`, `google-calendar`, `outlook`, `github`.

## Contract for the Jade client (Round C)

The client-side sign-in flows call exactly these endpoints; nothing else
is needed:

1. **Email auth**: `POST /v1/accounts/signup` then `POST /v1/accounts/login`.
   Store `session_token` in the OS keyring; send `Authorization: Bearer …`.
2. **Sign in with Google/Apple/Microsoft**: run the platform OIDC flow to
   obtain an `id_token` for this server's client id, then
   `POST /v1/auth/oidc/{provider}` with `{"id_token": …, "device_label": …}`.
3. **Sign in with GitHub**: OAuth code flow against *the server's* GitHub
   app (`GET /v1/connectors/github/authorize` shows the shape, but for
   login use the app's client id directly), then `POST /v1/auth/github`
   with `{"code": …}`. The server exchanges the code and links `github:<id>`.
4. **Connectors**: `GET /v1/connectors` → open the `authorize` URL in a
   browser → provider redirects to the server's `callback` → later
   `POST /v1/connectors/{id}/token` for a live access token.
5. **Pairing**: owner `POST /v1/pairing/create`, shows the `CORT-XXXXXX`
   code; new device `POST /v1/pairing/claim`; owner polls `GET …/status`
   and `POST …/accept` with an `endpoint_hint` (e.g. its P2P address);
   the new device then performs the direct handoff itself via
   `core/handoff.py` — the server never sees conversation content.

## Local API vs hosted API

- **On-device API** (Round A, `core/`): the CLI's own OpenAI-compatible
  server on loopback. Private, zero-network.
- **Hosted API** (this server, `/v1/chat/completions`): for thin clients
  and shared deployments; requests leave the device for the configured
  provider. The client must say which it's talking to.

## What's deliberately not here

- No Postgres/Redis *client code* — sqlite + in-memory backends behind a
  narrow interface; the swap is checklist item #1 in PRODUCTION.md.
- No Apple `client_secret` generation — the operator mints the ES256 JWT
  from their App Store Connect key; the endpoint shape is documented above.
- No email verification / password reset — real user lifecycle needs a
  mailer; flagged in PRODUCTION.md rather than faked.
