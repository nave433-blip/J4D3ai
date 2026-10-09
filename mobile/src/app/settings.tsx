/** Settings: server, account, connectors, TTS, model override, about. */
import { router } from 'expo-router';
import React, { useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Pressable,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
} from 'react-native';
import { ConnectorInfo } from '../api/client';
import {
  getModelOverride,
  getTtsEnabled,
  setModelOverride,
  setTtsEnabled,
} from '../store/conversations';
import { useSession } from '../store/sessionContext';
import { theme } from '../theme';

export default function SettingsScreen() {
  const { session, client, signOut } = useSession();
  const [tts, setTts] = useState(false);
  const [model, setModel] = useState('');
  const [connectors, setConnectors] = useState<ConnectorInfo[] | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    (async () => {
      setTts(await getTtsEnabled());
      setModel(await getModelOverride());
      if (client) {
        try {
          setConnectors(await client.connectors());
        } catch {
          setConnectors([]);
        }
      }
    })();
  }, [client]);

  const toggleTts = async (on: boolean) => {
    setTts(on);
    await setTtsEnabled(on);
  };

  const saveModel = async () => {
    await setModelOverride(model);
    Alert.alert('Saved', 'Model override updated. New chats will use it.');
  };

  const doSignOut = async () => {
    setBusy(true);
    try {
      await signOut();
    } finally {
      setBusy(false);
      router.replace('/auth');
    }
  };

  return (
    <ScrollView style={styles.root} contentContainerStyle={styles.content}>
      <Text style={styles.section}>Server</Text>
      <View style={styles.card}>
        <Text style={styles.rowLabel}>URL</Text>
        <Text style={styles.rowValue} selectable>
          {session.serverUrl ?? '—'}
        </Text>
        <Text style={styles.note}>
          To change servers, sign out and sign in again with the new address.
        </Text>
      </View>

      <Text style={styles.section}>Account</Text>
      <View style={styles.card}>
        <Text style={styles.rowLabel}>Name</Text>
        <Text style={styles.rowValue}>{session.user?.display_name ?? '—'}</Text>
        <Text style={[styles.rowLabel, styles.mt]}>Email</Text>
        <Text style={styles.rowValue}>{session.user?.email ?? '—'}</Text>
      </View>

      <Text style={styles.section}>Connectors on this server</Text>
      <View style={styles.card}>
        {connectors === null ? (
          <ActivityIndicator color={theme.accent} />
        ) : connectors.length === 0 ? (
          <Text style={styles.note}>Could not load connectors (or none configured).</Text>
        ) : (
          connectors.map((c) => (
            <View key={c.id} style={styles.connRow}>
              <Text style={styles.rowValue}>{c.label}</Text>
              <Text style={[styles.badge, c.connected ? styles.badgeOn : styles.badgeOff]}>
                {c.connected ? 'connected' : 'not connected'}
              </Text>
            </View>
          ))
        )}
        <Text style={styles.note}>
          Connectors are linked on the server (OAuth in a browser), not in this app.
        </Text>
      </View>

      <Text style={styles.section}>Voice & speech</Text>
      <View style={styles.card}>
        <View style={styles.switchRow}>
          <View style={styles.switchText}>
            <Text style={styles.rowValue}>Read replies aloud</Text>
            <Text style={styles.note}>Uses the phone’s text-to-speech.</Text>
          </View>
          <Switch value={tts} onValueChange={toggleTts} trackColor={{ true: theme.accent }} />
        </View>
        <Text style={styles.note}>
          Voice input (microphone button) works in the full EAS build via on-device speech
          recognition. In Expo Go it is hidden — use the keyboard’s dictation key instead.
        </Text>
      </View>

      <Text style={styles.section}>Model</Text>
      <View style={styles.card}>
        <Text style={styles.rowLabel}>Model override (optional)</Text>
        <TextInput
          style={styles.input}
          value={model}
          onChangeText={setModel}
          placeholder="Leave empty for the server default"
          placeholderTextColor={theme.textDim}
          autoCapitalize="none"
          autoCorrect={false}
        />
        <Pressable style={styles.smallBtn} onPress={saveModel}>
          <Text style={styles.smallBtnText}>Save</Text>
        </Pressable>
      </View>

      <Text style={styles.section}>About</Text>
      <View style={styles.card}>
        <Text style={styles.note}>
          Jade Mobile 1.0.0 — a thin client for your own Jade server. It stores no
          API keys and runs no AI on this phone; every reply comes from the server’s
          /v1/chat/completions endpoint. Chats are stored only on this device.
        </Text>
      </View>

      <Pressable style={styles.signOut} onPress={doSignOut} disabled={busy}>
        {busy ? (
          <ActivityIndicator color="#fff" />
        ) : (
          <Text style={styles.signOutText}>Sign out</Text>
        )}
      </Pressable>
      <View style={{ height: 40 }} />
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: theme.bg },
  content: { padding: 16 },
  section: {
    color: theme.textDim,
    fontSize: 13,
    fontWeight: '700',
    textTransform: 'uppercase',
    marginTop: 18,
    marginBottom: 8,
    letterSpacing: 0.5,
  },
  card: {
    backgroundColor: theme.bgElevated,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: theme.border,
    padding: 14,
  },
  rowLabel: { color: theme.textDim, fontSize: 13, marginBottom: 2 },
  rowValue: { color: theme.text, fontSize: 15 },
  note: { color: theme.textDim, fontSize: 12, lineHeight: 17, marginTop: 8 },
  mt: { marginTop: 10 },
  connRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: 8,
    borderBottomWidth: 1,
    borderBottomColor: theme.border,
  },
  badge: { fontSize: 11, fontWeight: '700', paddingHorizontal: 8, paddingVertical: 3, borderRadius: 10, overflow: 'hidden' },
  badgeOn: { color: '#0a0a12', backgroundColor: theme.success },
  badgeOff: { color: theme.textDim, backgroundColor: '#26263a' },
  switchRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  switchText: { flex: 1, paddingRight: 12 },
  input: {
    backgroundColor: theme.bgInput,
    borderWidth: 1,
    borderColor: theme.border,
    borderRadius: 10,
    color: theme.text,
    fontSize: 15,
    paddingHorizontal: 12,
    paddingVertical: 10,
    marginTop: 6,
  },
  smallBtn: {
    backgroundColor: '#2a2a44',
    borderRadius: 10,
    paddingVertical: 10,
    alignItems: 'center',
    marginTop: 10,
  },
  smallBtnText: { color: theme.text, fontWeight: '600' },
  signOut: {
    backgroundColor: '#5a2b2b',
    borderRadius: 14,
    paddingVertical: 14,
    alignItems: 'center',
    marginTop: 24,
  },
  signOutText: { color: '#fff', fontSize: 16, fontWeight: '700' },
});
