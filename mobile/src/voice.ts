/**
 * Voice: real on-device speech recognition (expo-speech-recognition) for
 * input, expo-speech for read-aloud.
 *
 * The recognition native module is loaded LAZILY — never statically
 * imported. It is absent in Expo Go (and any build without the native
 * module), so every entry point here degrades gracefully: no module means
 * no microphone button, and keyboard dictation still works.
 */
import { Alert } from 'react-native';
import * as Speech from 'expo-speech';

type NativeListener = { remove(): void };

/** Minimal surface of expo-speech-recognition's native module. */
type SpeechRecognitionModule = {
  requestPermissionsAsync(): Promise<{ granted: boolean }>;
  isRecognitionAvailable(): boolean;
  start(options: {
    lang?: string;
    interimResults?: boolean;
    addsPunctuation?: boolean;
    continuous?: boolean;
  }): void;
  stop(): void;
  addListener(event: 'result' | 'error' | 'end' | 'nomatch', cb: (e: any) => void): NativeListener;
};

let cachedMod: SpeechRecognitionModule | null | undefined;

function getModule(): SpeechRecognitionModule | null {
  if (cachedMod !== undefined) return cachedMod;
  try {
    // Deliberate lazy require: the native module may not exist (Expo Go), so
    // it must never be statically imported. The throw is the availability signal.
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const pkg = require('expo-speech-recognition');
    const mod = pkg?.ExpoSpeechRecognitionModule;
    cachedMod = mod && typeof mod.start === 'function' ? (mod as SpeechRecognitionModule) : null;
  } catch {
    cachedMod = null;
  }
  return cachedMod;
}

export function voiceUnavailableAlert(): void {
  Alert.alert(
    'Voice input unavailable',
    'This build does not include the native speech module. Use your keyboard’s dictation key instead.',
  );
}

/** True only when the native recognizer exists AND the device can recognize. */
export async function isVoiceInputAvailable(): Promise<boolean> {
  const mod = getModule();
  if (!mod) return false;
  try {
    return mod.isRecognitionAvailable() === true;
  } catch {
    return false;
  }
}

export interface VoiceCallbacks {
  /**
   * The recognizer's current best guess of the FULL utterance — interim
   * results first, then the final transcript. Replace the input text with
   * it; do not append.
   */
  onResult: (text: string) => void;
  /** The recognition session ended (final result, error, or silence). */
  onDone: () => void;
  onError: (message: string) => void;
}

function friendlyVoiceError(code: unknown): string {
  switch (code) {
    case 'not-allowed':
      return 'Microphone or speech-recognition permission was denied.';
    case 'no-speech':
    case 'speech-timeout':
      return 'No speech was detected. Try again.';
    case 'network':
      return 'A network problem stopped speech recognition.';
    case 'language-not-supported':
      return 'This language is not supported by the on-device recognizer.';
    case 'service-not-allowed':
      return 'Speech recognition is not available on this device.';
    default:
      return typeof code === 'string' && code
        ? `Speech recognition failed (${code}).`
        : 'Speech recognition failed.';
  }
}

/**
 * Ask for permission, then start listening. Resolves to a stop function, or
 * null when voice input is unavailable (an alert is shown in that case).
 * Calling the stop function requests a final result, which still arrives
 * through onResult.
 */
export async function startListening(cb: VoiceCallbacks): Promise<(() => Promise<void>) | null> {
  const mod = getModule();
  if (!mod) {
    voiceUnavailableAlert();
    return null;
  }

  let granted = false;
  try {
    granted = (await mod.requestPermissionsAsync())?.granted === true;
  } catch {
    granted = false;
  }
  if (!granted) {
    Alert.alert(
      'Microphone needed',
      'Allow microphone and speech-recognition access to dictate messages.',
    );
    return null;
  }

  const subs: NativeListener[] = [];
  let finished = false;
  let stopTimer: ReturnType<typeof setTimeout> | null = null;
  const finish = () => {
    if (finished) return;
    finished = true;
    if (stopTimer) {
      clearTimeout(stopTimer);
      stopTimer = null;
    }
    for (const s of subs) {
      try {
        s.remove();
      } catch {
        /* noop */
      }
    }
  };
  const handleDone = () => {
    if (finished) return;
    finish();
    cb.onDone();
  };

  subs.push(
    mod.addListener('result', (event: any) => {
      const text = event?.results?.[0]?.transcript;
      if (typeof text === 'string' && text.length > 0) cb.onResult(text);
    }),
  );
  subs.push(
    mod.addListener('error', (event: any) => {
      const code = event?.code;
      if (code !== 'aborted') cb.onError(friendlyVoiceError(code));
      handleDone();
    }),
  );
  // 'end' fires after stop(); 'nomatch' when nothing recognizable was heard.
  subs.push(mod.addListener('end', handleDone));
  subs.push(mod.addListener('nomatch', handleDone));

  try {
    mod.start({ lang: 'en-US', interimResults: true, addsPunctuation: true, continuous: false });
  } catch {
    finish();
    cb.onError('Could not start speech recognition on this device.');
    cb.onDone();
    return null;
  }

  return async () => {
    try {
      mod.stop(); // asks the recognizer for a final result via onResult
    } catch {
      /* noop */
    }
    // Safety net: if the native 'end' event never arrives, end locally.
    stopTimer = setTimeout(handleDone, 2000);
  };
}

// ---------------------------------------------------------------------------
// Read-aloud (text to speech) — expo-speech, works everywhere incl. Expo Go.
// ---------------------------------------------------------------------------

export async function speak(text: string): Promise<void> {
  try {
    const speaking = await Speech.isSpeakingAsync();
    if (speaking) {
      await Speech.stop();
      return;
    }
    await Speech.speak(text, { rate: 1.0 });
  } catch {
    /* TTS unavailable — stay silent */
  }
}

export async function stopSpeaking(): Promise<void> {
  try {
    await Speech.stop();
  } catch {
    /* noop */
  }
}
