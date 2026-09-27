/** Auth session persistence. Bearer token + server URL live in the OS keychain. */
import * as SecureStore from 'expo-secure-store';
import type { CortanaUser } from '../api/client';

const K_URL = 'cortana.serverUrl';
const K_TOKEN = 'cortana.sessionToken';
const K_USER = 'cortana.user';

export interface Session {
  serverUrl: string | null;
  token: string | null;
  user: CortanaUser | null;
}

export async function loadSession(): Promise<Session> {
  const [serverUrl, token, userRaw] = await Promise.all([
    SecureStore.getItemAsync(K_URL),
    SecureStore.getItemAsync(K_TOKEN),
    SecureStore.getItemAsync(K_USER),
  ]);
  let user: CortanaUser | null = null;
  if (userRaw) {
    try {
      user = JSON.parse(userRaw) as CortanaUser;
    } catch {
      user = null;
    }
  }
  return { serverUrl, token, user };
}

export async function saveSession(s: Session): Promise<void> {
  await Promise.all([
    s.serverUrl ? SecureStore.setItemAsync(K_URL, s.serverUrl) : SecureStore.deleteItemAsync(K_URL),
    s.token ? SecureStore.setItemAsync(K_TOKEN, s.token) : SecureStore.deleteItemAsync(K_TOKEN),
    s.user
      ? SecureStore.setItemAsync(K_USER, JSON.stringify(s.user))
      : SecureStore.deleteItemAsync(K_USER),
  ]);
}

export async function clearSession(): Promise<void> {
  await Promise.all([
    SecureStore.deleteItemAsync(K_URL),
    SecureStore.deleteItemAsync(K_TOKEN),
    SecureStore.deleteItemAsync(K_USER),
  ]);
}
