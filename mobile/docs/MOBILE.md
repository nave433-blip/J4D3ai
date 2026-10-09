# Cortana Mobile (Android + iOS)

One Expo React Native codebase (TypeScript, managed workflow) that builds both
the Android and iOS apps.

## What this app is — read this first

**Cortana Mobile is a thin client. It does NOT run Cortana on your phone.**
There is no Python brain, no model, and no API key inside this app. Every chat
message is sent to **your own Cortana server** (`server/` in this repo), which
relays it to whatever chat provider you configured there. If the server is
down, the app can't chat — that's by design.

What the app stores:
- Server URL + session bearer token + your profile → OS keychain
  (`expo-secure-store`).
- Conversations → on this device only (`AsyncStorage`). They are never
  uploaded anywhere.

## 1. Run your Cortana server

On the computer that will serve the app (your PC, a home server, a VPS —
anything reachable from your phone):

```bash
cd <cortana-repo>            # the repo root, NOT mobile/
cp .env.example .env         # then edit .env: set secrets (CORTANA_TOKEN_KEY at minimum)
pip install -r server/requirements.txt
CORTANA_DATA_DIR=./server-data CORTANA_TOKEN_KEY="$(openssl rand -base64 32)" \
  python -m server.app
```

Or with Docker (see `server/README.md`):

```bash
docker compose -f server/docker-compose.yml up --build
```

Sanity check from that machine:

```bash
curl localhost:8080/healthz
# {"ok": true, "version": "0.1.0"}
```

Create your account (do this once):

```bash
curl -X POST localhost:8080/v1/accounts/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"a-very-long-passphrase","display_name":"You"}'
```

For chat to work, the server needs a provider (`.env`):
`CORTANA_CHAT_BASE_URL`, `CORTANA_CHAT_API_KEY`, and optionally
`CORTANA_CHAT_MODEL`.

**Reaching the server from your phone:** the app needs an `http(s)://` URL
that resolves from the phone. On the same Wi-Fi, use the computer's LAN IP,
e.g. `http://192.168.1.10:8080` (not `localhost` — that's the phone itself).
Over the internet, put the server behind HTTPS (reverse proxy, Tailscale,
Cloudflare Tunnel…) and use that URL. Plain HTTP on a trusted LAN is fine;
don't expose unauthenticated HTTP to the internet.

## 2. Run the app in development

Prereqs: Node 20+, the Expo CLI (bundled via npx).

```bash
cd mobile
npx expo install            # SDK-compatible deps (first time)
npx expo start              # scan the QR with Expo Go (Android) or Camera (iOS)
```

Note: voice *input* (mic button) needs native code, so it is hidden in Expo
Go. Voice input appears in development/production builds. Read-aloud works
everywhere. In Expo Go, use your keyboard's dictation key to talk.

## 3. Build with EAS (APK / AAB / IPA)

You need:
1. **An Expo account** (free) — https://expo.dev/signup
2. **For iOS: an Apple Developer Program membership** ($99/year). Apple does
   not allow installing unsigned IPAs on real iPhones; there is no way around
   this.
3. **For Android:** nothing paid. A Google Play developer account ($25
   one-time) is only needed if you publish to the Play Store; for sideloading
   an APK you just need the file.

Install and log in (use `npx eas-cli@latest` — never a bare `eas`):

```bash
cd mobile
npx eas-cli@latest login
```

Builds:

```bash
# Android APK you can install directly (sideload) — easiest first build
npx eas-cli@latest build --profile preview --platform android

# Android App Bundle for the Play Store
npx eas-cli@latest build --profile production --platform android

# iOS — needs the Apple Developer membership above
npx eas-cli@latest build --profile production --platform ios
```

When a build finishes, EAS gives you a download link (and QR code) for the
artifact. Install the APK on Android via `adb install` or by opening the file
on the phone.

Submit to stores (optional):

```bash
npx eas-cli@latest submit --platform android   # needs Play Console service-account key
npx eas-cli@latest submit --platform ios       # needs App Store Connect API key
```

**What this Linux VM cannot do:** produce a *signed* iOS IPA. IPA signing
requires Xcode/macOS (or EAS cloud build with your Apple Developer account).
This project is build-ready — the native config, bundle IDs
(`com.nave433.cortana`), permissions, and icons are all in place — but the
actual signed binaries must come from `npx eas-cli@latest build`.

## 4. Project layout

```
mobile/
  app.json            Expo config (name, slug, bundle IDs, icons, permissions,
                      plugins: expo-router, expo-secure-store,
                      expo-splash-screen, expo-speech-recognition)
  eas.json            EAS build profiles (development / preview APK /
                      production AAB+IPA)
  package.json        main: expo-router/entry
  src/
    app/
      _layout.tsx     Stack navigator + session provider
      index.tsx       boot gate (validates stored token → /chat or /auth)
      auth.tsx        server URL + login/signup
      chat.tsx        Grok/Gemini-style chat: drawer, thread, composer
      settings.tsx    server, account, connectors, TTS, model override, about
    api/client.ts     Cortana server HTTP client (auth, connectors, chat;
                      streaming via XHR-parsed SSE, plain-JSON fallback)
    store/            secure session (keychain) + conversations (AsyncStorage)
    components/       MessageBubble (markdown), Composer, ConversationDrawer
    voice.ts          expo-speech TTS + lazy expo-speech-recognition input
    theme.ts
  assets/             icon.png, adaptive-icon.png, splash.png, favicon.png
  assets-src/         1600px master artwork (source of the derived assets)
  scripts/
    make_icons.py     derive assets from the master (Pillow)
    check_icons.py    CI-style assert on asset existence + dimensions
  docs/MOBILE.md      this file
```

## 5. Feature truth table

| Feature | Real? | Notes |
|---|---|---|
| Chat with streaming replies | ✅ real | SSE from your server's `/v1/chat/completions`; falls back to a single plain-JSON completion when streaming yields nothing usable |
| Markdown rendering | ✅ real | code blocks, tables, lists |
| Login / signup / logout | ✅ real | server's `/v1/accounts/*`, token in keychain |
| Conversation history | ✅ real | device-only, 100-chat cap |
| Connector status list | ✅ real | read-only; linking happens in a browser on the server |
| Read replies aloud | ✅ real | `expo-speech`, on-device TTS, toggle in Settings |
| Voice input (mic) | ✅ real, **EAS builds only** | on-device speech recognition via the official `expo-speech-recognition` package; hidden in Expo Go where the native module can't exist |
| Stop button | ✅ real | cancels the in-flight request (stream abort / fetch abort), not just a local flag |
| Running Cortana locally on the phone | ❌ not real, not claimed | the app is a client; the server does the work |
| Offline chat | ❌ | no server, no replies |

## 6. Troubleshooting

- **"Could not reach the server"** — the URL is wrong, the server isn't
  running, or the phone can't route to it. From the phone's browser, open
  `<server-url>/healthz`; you should see `{"ok": true, ...}`.
- **Login loops back to the auth screen** — the stored token expired or the
  server DB was wiped. Just log in again.
- **501 "No chat provider configured"** — set `CORTANA_CHAT_BASE_URL` /
  `CORTANA_CHAT_API_KEY` in the server's `.env` and restart it.
- **Mic button missing** — you're in Expo Go. It's intentional (see above).
- **`npx expo-doctor` warnings** — see "Known verification notes" below.

## Known verification notes

- `npx tsc --noEmit` — clean (2026-09-27).
- `npx expo lint` — clean (2026-09-27).
- `npx expo-doctor` — 21/21 checks pass (2026-09-27).
- Icons: `python3 scripts/check_icons.py` asserts all four assets at exact
  dimensions.
- Native plugin validation: ran `npx expo prebuild --platform android` on a
  scratch copy (deleted afterwards — `android/`/`ios/` are never committed).
  The `expo-speech-recognition` plugin lands `RECORD_AUDIO` plus the speech
  service `<queries>` in the manifest; the `expo-splash-screen` plugin
  generates splash drawables at all densities. iOS native generation needs
  macOS and is left to EAS (the plugin source sets the iOS plist strings).
- Prebuild prints an informational note: `userInterfaceStyle: Install
  expo-system-ui in your project to enable this feature.` The app hardcodes
  its dark theme on every screen, so this is cosmetic-only and left out.
- No signed APK/AAB/IPA was produced here: that requires
  `npx eas-cli@latest build` with your Expo account (and Apple Developer
  membership for iOS).
