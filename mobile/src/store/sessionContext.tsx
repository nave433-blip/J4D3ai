/** App-wide auth session: secure-stored server URL + bearer token + user. */
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { CortanaClient } from '../api/client';
import { clearSession, loadSession, saveSession, Session } from './session';

interface SessionCtx {
  ready: boolean;
  session: Session;
  client: CortanaClient | null;
  signIn: (serverUrl: string, email: string, password: string) => Promise<void>;
  signUp: (serverUrl: string, email: string, password: string, displayName: string) => Promise<void>;
  signOut: () => Promise<void>;
  refreshMe: () => Promise<void>;
}

const Ctx = createContext<SessionCtx | null>(null);

function normalizeUrl(raw: string): string {
  let u = raw.trim();
  if (!u) throw new Error('Enter your server URL.');
  if (!/^https?:\/\//i.test(u)) u = `http://${u}`;
  return u.replace(/\/+$/, '');
}

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [session, setSession] = useState<Session>({ serverUrl: null, token: null, user: null });

  useEffect(() => {
    (async () => {
      const s = await loadSession();
      // Validate a stored token against the server before trusting it.
      if (s.serverUrl && s.token) {
        try {
          const me = await new CortanaClient(s.serverUrl, s.token).me();
          const fresh = { ...s, user: me };
          await saveSession(fresh);
          setSession(fresh);
        } catch {
          await clearSession();
          setSession({ serverUrl: s.serverUrl, token: null, user: null });
        }
      } else {
        setSession(s);
      }
      setReady(true);
    })();
  }, []);

  const client = useMemo(
    () => (session.serverUrl ? new CortanaClient(session.serverUrl, session.token) : null),
    [session.serverUrl, session.token],
  );

  const signIn = useCallback(async (serverUrlRaw: string, email: string, password: string) => {
    const serverUrl = normalizeUrl(serverUrlRaw);
    if (!email.trim()) throw new Error('Enter your email.');
    if (!password) throw new Error('Enter your password.');
    const probe = new CortanaClient(serverUrl);
    await probe.health(); // friendly failure if the URL is wrong
    const { user, token } = await probe.login(email.trim(), password);
    const s = { serverUrl, token, user };
    await saveSession(s);
    setSession(s);
  }, []);

  const signUp = useCallback(
    async (serverUrlRaw: string, email: string, password: string, displayName: string) => {
      const serverUrl = normalizeUrl(serverUrlRaw);
      if (!email.trim()) throw new Error('Enter your email.');
      if (password.length < 12) throw new Error('Password must be at least 12 characters.');
      const probe = new CortanaClient(serverUrl);
      await probe.health();
      await probe.signup(email.trim(), password, displayName.trim() || 'Me');
      // Server signup does not return a token — log in right away.
      await signIn(serverUrl, email.trim(), password);
    },
    [signIn],
  );

  const signOut = useCallback(async () => {
    if (client) await client.logout();
    await clearSession();
    setSession({ serverUrl: null, token: null, user: null });
  }, [client]);

  const refreshMe = useCallback(async () => {
    if (!client) return;
    const me = await client.me();
    const s = { ...session, user: me };
    await saveSession(s);
    setSession(s);
  }, [client, session]);

  const value = useMemo(
    () => ({ ready, session, client, signIn, signUp, signOut, refreshMe }),
    [ready, session, client, signIn, signUp, signOut, refreshMe],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): SessionCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useSession must be used inside SessionProvider');
  return ctx;
}
