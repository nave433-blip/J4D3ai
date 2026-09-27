/**
 * Voice: HONEST implementation only.
 *
 * - Speech OUTPUT (read-aloud) uses expo-speech — real, on-device, works
 *   everywhere including Expo Go.
 * - Speech INPUT uses @react-native-voice/voice (real on-device speech
 *   recognition). That package ships native code, so it is ONLY present in
 *   EAS/dev builds — never in Expo Go. The mic button is rendered only when
 *   the native module is actually available; otherwise voice input is left
 *   to the OS keyboard's dictation key, and the docs say so.
 */
import { Alert, PermissionsAndroid, Platform } from 'react-native';
import * as Speech from 'expo-speech';

// ---------------------------------------------------------------------------
// lazy native voice module (never imported statically — Expo Go would crash)
// ---------------------------------------------------------------------------

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare function require(name: string): any;

let cached: unknown = undefined;

function getVoiceModule(): unknown {
  if (cached !== undefined) return cached;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const mod = require('@react-native-voice/voice');
    cached = mod?.default ?? mod ?? null;
  } catch {
    cached = null; // native module not linked (e.g. Expo Go)
  }
  return cached;
}

export async function isVoiceInputAvailable(): Promise<boolean> {
  const V = getVoiceModule() as {
    isAvailable?: () => Promise<number>;
  } | null;
  if (!V) return false;
  try {
    if (typeof V.isAvailable === 'function') {
      const r = await V.isAvailable();
      return r === 1 || (r as unknown) === true;
    }
    return true;
  } catch {
    return false;
  }
}

async function ensureMicPermission(): Promise<boolean> {
  if (Platform.OS === 'android') {
    try {
      const granted = await PermissionsAndroid.request(
        PermissionsAndroid.PERMISSIONS.RECORD_AUDIO,
        {
          title: 'Microphone access',
          message: 'Cortana needs the microphone to transcribe your voice.',
          buttonPositive: 'OK',
        },
      );
      return granted === PermissionsAndroid.RESULTS.GRANTED;
    } catch {
      return false;
    }
  }
  // iOS prompts automatically on first use (InfoPlist usage descriptions).
  return true;
}

export interface VoiceCallbacks {
  onResult: (text: string) => void;
  onDone: () => void;
  onError: (message: string) => void;
}

/** Start one-shot speech recognition. Returns a stop() function. */
export async function startListening(cb: VoiceCallbacks): Promise<() => Promise<void>> {
  const V = getVoiceModule() as {
    start: (locale: string) => Promise<void>;
    stop: () => Promise<void>;
    destroy: () => Promise<void>;
    removeAllListeners: () => void;
    onSpeechResults?: unknown;
    onSpeechEnd?: unknown;
    onSpeechError?: unknown;
  };
  if (!V) throw new Error('Voice input is not available in this build.');

  const ok = await ensureMicPermission();
  if (!ok) {
    cb.onError('Microphone permission was denied.');
    return async () => {};
  }

  V.removeAllListeners();
  V.onSpeechResults = (e: { value?: string[] }) => {
    const text = e?.value?.[0] ?? '';
    if (text) cb.onResult(text);
  };
  const finish = () => {
    try {
      V.removeAllListeners();
    } catch {
      /* ignore */
    }
    cb.onDone();
  };
  V.onSpeechEnd = finish;
  V.onSpeechError = (e: { error?: { message?: string } }) => {
    cb.onError(e?.error?.message || 'Speech recognition failed.');
    finish();
  };

  try {
    await V.destroy().catch(() => {});
    await V.start('en-US');
  } catch (err) {
    finish();
    throw err instanceof Error ? err : new Error('Could not start speech recognition.');
  }

  let stopped = false;
  return async () => {
    if (stopped) return;
    stopped = true;
    try {
      await V.stop();
    } catch {
      /* ignore */
    }
    try {
      await V.destroy();
    } catch {
      /* ignore */
    }
  };
}

export function voiceUnavailableAlert(): void {
  Alert.alert(
    'Voice input needs the full build',
    'Speech recognition uses native code, so it only works in the EAS production/development build — not in Expo Go. ' +
      'In the meantime, use your keyboard\u2019s dictation (microphone) key to talk instead of typing.',
  );
}

// ---------------------------------------------------------------------------
// speech output (expo-speech — real, everywhere)
// ---------------------------------------------------------------------------

/** Strip markdown noise so read-aloud sounds natural. */
export function stripMarkdown(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, ' code block omitted ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/[*_~]{1,3}/g, '')
    .trim();
}

export function speak(text: string): void {
  try {
    Speech.stop();
    const clean = stripMarkdown(text);
    if (clean) Speech.speak(clean);
  } catch {
    /* TTS unavailable — stay silent rather than crash */
  }
}

export function stopSpeaking(): void {
  try {
    Speech.stop();
  } catch {
    /* ignore */
  }
}
