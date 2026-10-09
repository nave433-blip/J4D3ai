/** Bottom input bar: text field + mic (only when real) + send. */
import React, { useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Keyboard,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { theme } from '../theme';
import { isVoiceInputAvailable, startListening, voiceUnavailableAlert } from '../voice';

interface Props {
  onSend: (text: string) => void;
  busy: boolean;
  onStop?: () => void;
}

export function Composer({ onSend, busy, onStop }: Props) {
  const [text, setText] = useState('');
  const [voiceOk, setVoiceOk] = useState(false);
  const [listening, setListening] = useState(false);
  const stopRef = useRef<(() => Promise<void>) | null>(null);

  useEffect(() => {
    let live = true;
    isVoiceInputAvailable().then((ok) => {
      if (live) setVoiceOk(ok);
    });
    return () => {
      live = false;
      stopRef.current?.().catch(() => {});
    };
  }, []);

  const submit = () => {
    const t = text.trim();
    if (!t || busy) return;
    setText('');
    Keyboard.dismiss();
    onSend(t);
  };

  const toggleListen = async () => {
    if (!voiceOk) {
      voiceUnavailableAlert();
      return;
    }
    if (listening) {
      await stopRef.current?.().catch(() => {});
      setListening(false);
      return;
    }
    try {
      setListening(true);
      stopRef.current = await startListening({
        // Interim results are the recognizer's current best guess of the
        // whole utterance — replace the input text, don't append.
        onResult: (chunk) => setText(chunk),
        onDone: () => setListening(false),
        onError: (message) => {
          setListening(false);
          Alert.alert('Voice input failed', message);
        },
      });
    } catch {
      setListening(false);
      voiceUnavailableAlert();
    }
  };

  return (
    <View style={styles.wrap}>
      <View style={styles.bar}>
        {voiceOk ? (
          <Pressable
            onPress={toggleListen}
            style={[styles.iconBtn, listening && styles.listening]}
            accessibilityLabel={listening ? 'Stop listening' : 'Voice input'}
          >
            {listening ? (
              <ActivityIndicator size="small" color="#fff" />
            ) : (
              <Text style={styles.iconText}>🎙</Text>
            )}
          </Pressable>
        ) : null}
        <TextInput
          style={styles.input}
          value={text}
          onChangeText={setText}
          placeholder={listening ? 'Listening…' : 'Message Jade'}
          placeholderTextColor={theme.textDim}
          multiline
          maxLength={8000}
          editable={!busy}
          onSubmitEditing={submit}
          blurOnSubmit={false}
          returnKeyType="send"
        />
        {busy ? (
          <Pressable onPress={onStop} style={[styles.iconBtn, styles.stopBtn]} accessibilityLabel="Stop">
            <Text style={styles.iconText}>⏹</Text>
          </Pressable>
        ) : (
          <Pressable
            onPress={submit}
            style={[styles.iconBtn, styles.sendBtn, !text.trim() && styles.sendBtnDisabled]}
            disabled={!text.trim()}
            accessibilityLabel="Send"
          >
            <Text style={styles.iconText}>↑</Text>
          </Pressable>
        )}
      </View>
      {listening ? <Text style={styles.listeningHint}>Listening… tap the mic to stop</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { paddingHorizontal: 12, paddingBottom: 14, paddingTop: 6, backgroundColor: theme.bg },
  bar: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    backgroundColor: theme.bgInput,
    borderRadius: 24,
    borderWidth: 1,
    borderColor: theme.border,
    paddingHorizontal: 6,
    paddingVertical: 6,
  },
  input: {
    flex: 1,
    color: theme.text,
    fontSize: 16,
    maxHeight: 140,
    paddingHorizontal: 10,
    paddingVertical: 8,
  },
  iconBtn: {
    width: 40,
    height: 40,
    borderRadius: 20,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: '#26263a',
  },
  iconText: { color: theme.text, fontSize: 18 },
  sendBtn: { backgroundColor: theme.accent },
  sendBtnDisabled: { backgroundColor: '#2a2a40', opacity: 0.5 },
  stopBtn: { backgroundColor: '#5a2b2b' },
  listening: { backgroundColor: theme.danger },
  listeningHint: { color: theme.textDim, fontSize: 12, textAlign: 'center', marginTop: 6 },
});
