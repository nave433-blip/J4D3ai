/**
 * HTTP client for the Jade reference cloud server (`server/` in the repo).
 *
 * The app is a THIN CLIENT: it never bundles the Python brain. Every chat
 * message goes to YOUR server's OpenAI-compatible endpoint, which relays to
 * whatever provider you configured with CORTANA_CHAT_*.
 *
 * Server routes used:
 *   POST /v1/accounts/signup   {email, password, display_name} -> 201 {user}
 *   POST /v1/accounts/login    {email, password, device_label} -> {user, session_token}
 *   POST /v1/accounts/logout   (Bearer) -> {ok: true}
 *   GET  /v1/accounts/me       (Bearer) -> {user: {id, email, display_name}}
 *   GET  /v1/connectors        (Bearer) -> {connectors: [...]}
 *   POST /v1/chat/completions  (Bearer) OpenAI-compatible; stream=true gives
 *                              SSE `data:` chunks ending in `data: [DONE]`
 *   GET  /healthz -> {ok, version}
 */
import { Platform } from 'react-native';

export interface JadeUser {
  id: number;
  email: string;
  display_name: string;
}

export interface ChatMsg {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

export interface ConnectorInfo {
  id: string;
  label: string;
  provider: string;
  connected: boolean;
  scopes: string[];
}

export class ServerError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function friendlyError(status: number, serverMsg: string): string {
  if (status === 0) return 'Could not reach the server. Check the server URL and your connection.';
  if (status === 401) return serverMsg || 'Not authorized. Your session may have expired — log in again.';
  if (status === 400) return serverMsg || 'The server rejected the request.';
  if (status === 429) return 'Rate limited — wait a minute and try again.';
  if (status === 501) return 'No chat provider configured on the server (set CORTANA_CHAT_BASE_URL and CORTANA_CHAT_API_KEY).';
  if (status >= 500) return `Server error (${status}). Try again in a bit.`;
  return serverMsg || `Request failed (${status}).`;
}

async function parseError(res: Response): Promise<string> {
  let msg = '';
  try {
    const body = await res.json();
    if (body && typeof body.error === 'string') msg = body.error;
  } catch {
    /* non-JSON error body */
  }
  return friendlyError(res.status, msg);
}

export class JadeClient {
  constructor(
    public baseUrl: string,
    public token: string | null = null,
  ) {}

  private get url(): string {
    return this.baseUrl.replace(/\/+$/, '');
  }

  private headers(json = true): Record<string, string> {
    const h: Record<string, string> = {};
    if (json) h['Content-Type'] = 'application/json';
    if (this.token) h['Authorization'] = `Bearer ${this.token}`;
    return h;
  }

  async health(): Promise<{ ok: boolean; version?: string }> {
    const res = await fetch(`${this.url}/healthz`);
    if (!res.ok) throw new ServerError(await parseError(res), res.status);
    return res.json();
  }

  async signup(email: string, password: string, displayName: string): Promise<JadeUser> {
    const res = await fetch(`${this.url}/v1/accounts/signup`, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify({ email, password, display_name: displayName }),
    });
    if (!res.ok) throw new ServerError(await parseError(res), res.status);
    const body = await res.json();
    return body.user as JadeUser;
  }

  async login(email: string, password: string): Promise<{ user: JadeUser; token: string }> {
    const res = await fetch(`${this.url}/v1/accounts/login`, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify({
        email,
        password,
        device_label: `Jade Mobile (${Platform.OS})`,
      }),
    });
    if (!res.ok) throw new ServerError(await parseError(res), res.status);
    const body = await res.json();
    return { user: body.user as JadeUser, token: body.session_token as string };
  }

  async logout(): Promise<void> {
    try {
      await fetch(`${this.url}/v1/accounts/logout`, {
        method: 'POST',
        headers: this.headers(false),
      });
    } catch {
      /* best effort — local session is cleared regardless */
    }
  }

  async me(): Promise<JadeUser> {
    const res = await fetch(`${this.url}/v1/accounts/me`, { headers: this.headers(false) });
    if (!res.ok) throw new ServerError(await parseError(res), res.status);
    const body = await res.json();
    return body.user as JadeUser;
  }

  async connectors(): Promise<ConnectorInfo[]> {
    const res = await fetch(`${this.url}/v1/connectors`, { headers: this.headers(false) });
    if (!res.ok) throw new ServerError(await parseError(res), res.status);
    const body = await res.json();
    return (body.connectors ?? []) as ConnectorInfo[];
  }

  /**
   * Non-streaming chat completion. Returns the assistant's full reply text.
   * Accepts an AbortSignal so the UI stop button can cancel it for real.
   */
  async chatOnce(messages: ChatMsg[], model?: string, signal?: AbortSignal): Promise<string> {
    const res = await fetch(`${this.url}/v1/chat/completions`, {
      method: 'POST',
      headers: this.headers(),
      signal,
      body: JSON.stringify({
        model: model || undefined,
        messages,
        stream: false,
      }),
    });
    if (!res.ok) throw new ServerError(await parseError(res), res.status);
    const body = await res.json();
    const content = body?.choices?.[0]?.message?.content;
    if (typeof content !== 'string') throw new ServerError('Unexpected chat response from server.', 502);
    return content;
  }

  /**
   * Streaming chat via XMLHttpRequest progressive reads (React Native's
   * fetch does not expose a streaming body). Parses the server's SSE
   * `data:` chunks — OpenAI delta format — until `data: [DONE]`.
   *
   * Returns a { promise, abort } pair: abort() cancels the in-flight
   * request for real (the UI stop button uses it).
   *
   * If the server answers 200 but the body is a complete JSON completion
   * instead of SSE (some proxies buffer event streams), the JSON is parsed
   * and delivered through onToken so the stream still yields text.
   */
  streamChat(opts: {
    messages: ChatMsg[];
    model?: string;
    onToken: (text: string) => void;
    timeoutMs?: number;
  }): { promise: Promise<void>; abort: () => void } {
    const { messages, model, onToken, timeoutMs = 120000 } = opts;
    let xhr: XMLHttpRequest | null = null;
    let userAborted = false;

    const promise = new Promise<void>((resolve, reject) => {
      xhr = new XMLHttpRequest();
      const req: XMLHttpRequest = xhr;
      let settled = false;
      const done = (fn: () => void) => {
        if (!settled) {
          settled = true;
          fn();
        }
      };

      const timer: ReturnType<typeof setTimeout> = setTimeout(() => {
        try {
          req.abort();
        } catch {
          /* ignore */
        }
        done(() => reject(new ServerError('Chat request timed out.', 0)));
      }, timeoutMs);

      const clearTimer = () => clearTimeout(timer);

      let seen = 0;
      let buffer = '';
      let gotToken = false;
      const emit = (text: string) => {
        if (text.length > 0) {
          gotToken = true;
          onToken(text);
        }
      };
      const handleProgress = () => {
        try {
          const text: string = (req as unknown as { responseText: string }).responseText || '';
          const chunk = text.slice(seen);
          seen = text.length;
          buffer += chunk;
          const lines = buffer.split('\n');
          buffer = lines.pop() ?? '';
          for (const line of lines) {
            const t = line.trim();
            if (!t.startsWith('data:')) continue;
            const data = t.slice(5).trim();
            if (!data || data === '[DONE]') continue;
            try {
              const json = JSON.parse(data);
              const content = json?.choices?.[0]?.delta?.content;
              if (typeof content === 'string') emit(content);
            } catch {
              /* partial JSON at a chunk boundary — next progress event completes it */
            }
          }
        } catch {
          /* never let a parse race kill the stream */
        }
      };

      req.open('POST', `${this.url}/v1/chat/completions`);
      req.setRequestHeader('Content-Type', 'application/json');
      if (this.token) req.setRequestHeader('Authorization', `Bearer ${this.token}`);
      req.onprogress = handleProgress;
      req.onload = () => {
        clearTimer();
        handleProgress(); // flush any trailing buffered lines
        if (req.status >= 200 && req.status < 300) {
          if (!gotToken) {
            // Not SSE after all? Try a whole-body JSON completion (buffered proxy).
            try {
              const body = JSON.parse(
                (req as unknown as { responseText: string }).responseText || 'null',
              );
              const content = body?.choices?.[0]?.message?.content;
              if (typeof content === 'string') emit(content);
            } catch {
              /* leave it tokenless — the caller decides the fallback */
            }
          }
          done(() => resolve());
        } else {
          let msg = '';
          try {
            const body = JSON.parse(
              (req as unknown as { responseText: string }).responseText || '{}',
            );
            if (typeof body.error === 'string') msg = body.error;
          } catch {
            /* ignore */
          }
          const friendly = friendlyError(req.status, msg);
          done(() => reject(new ServerError(friendly, req.status)));
        }
      };
      req.onerror = () => {
        clearTimer();
        done(() => reject(new ServerError(friendlyError(0, ''), 0)));
      };
      req.onabort = () => {
        clearTimer();
        done(() =>
          reject(
            new ServerError(
              userAborted ? 'Chat request was cancelled.' : 'Chat request was aborted.',
              0,
            ),
          ),
        );
      };
      req.send(
        JSON.stringify({
          model: model || undefined,
          messages,
          stream: true,
        }),
      );
    });

    return {
      promise,
      abort() {
        userAborted = true;
        try {
          xhr?.abort();
        } catch {
          /* ignore */
        }
      },
    };
  }
}
