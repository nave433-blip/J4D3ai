/** Local conversation history (device-only, AsyncStorage — never sent anywhere). */
import AsyncStorage from '@react-native-async-storage/async-storage';

export interface StoredMessage {
  role: 'user' | 'assistant';
  content: string;
  ts: number;
}

export interface Conversation {
  id: string;
  title: string;
  messages: StoredMessage[];
  updatedAt: number;
}

const KEY = 'jade.conversations.v1';
const PREF_TTS = 'jade.ttsEnabled';
const PREF_MODEL = 'jade.modelOverride';

export function newId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function titleFor(firstUserText: string): string {
  const t = firstUserText.replace(/\s+/g, ' ').trim();
  return t.length > 42 ? `${t.slice(0, 42)}…` : t || 'New chat';
}

export async function loadConversations(): Promise<Conversation[]> {
  try {
    const raw = await AsyncStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as Conversation[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export async function saveConversations(list: Conversation[]): Promise<void> {
  // Cap at 100 conversations to bound storage.
  const trimmed = [...list]
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, 100);
  await AsyncStorage.setItem(KEY, JSON.stringify(trimmed));
}

export async function getTtsEnabled(): Promise<boolean> {
  return (await AsyncStorage.getItem(PREF_TTS)) === '1';
}

export async function setTtsEnabled(on: boolean): Promise<void> {
  await AsyncStorage.setItem(PREF_TTS, on ? '1' : '0');
}

export async function getModelOverride(): Promise<string> {
  return (await AsyncStorage.getItem(PREF_MODEL)) ?? '';
}

export async function setModelOverride(model: string): Promise<void> {
  await AsyncStorage.setItem(PREF_MODEL, model.trim());
}
