/** First-run: point at YOUR Cortana server, then sign up or log in. */
import { router } from 'expo-router';
import React, { useState } from 'react';
import {
  ActivityIndicator,
  Image,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { useSession } from '../src/store/sessionContext';
import { theme } from '../src/theme';

export default function AuthScreen() {
  const { session, signIn, signUp } = useSession();
  const [mode, setMode] = useState<'login' | 'signup'>('login');
  const [serverUrl, setServerUrl] = useState(session.serverUrl ?? '');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async () => {
    setBusy(true);
    setError('');
    try {
      if (mode === 'login') {
        await signIn(serverUrl, email, password);
      } else {
        await signUp(serverUrl, email, password, displayName);
      }
      router.replace('/chat');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <KeyboardAvoidingView
      style={styles.root}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <ScrollView contentContainerStyle={styles.scroll} keyboardShouldPersistTaps="handled">
        <Image source={require('../assets/icon.png')} style={styles.logo} />
        <Text style={styles.title}>Cortana</Text>
        <Text style={styles.subtitle}>
          Your assistant, running on{'\n'}your own server — not in this app.
        </Text>

        <Text style={styles.label}>Cortana server URL</Text>
        <TextInput
          style={styles.input}
          value={serverUrl}
          onChangeText={setServerUrl}
          placeholder="http://192.168.1.10:8080"
          placeholderTextColor={theme.textDim}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
        />
        <Text style={styles.hint}>
          Run the server on your computer first (see the setup guide), then enter its address here.
        </Text>

        <View style={styles.tabs}>
          {(['login', 'signup'] as const).map((m) => (
            <Pressable
              key={m}
              onPress={() => {
                setMode(m);
                setError('');
              }}
              style={[styles.tab, mode === m && styles.tabActive]}
            >
              <Text style={[styles.tabText, mode === m && styles.tabTextActive]}>
                {m === 'login' ? 'Log in' : 'Sign up'}
              </Text>
            </Pressable>
          ))}
        </View>

        {mode === 'signup' ? (
          <>
            <Text style={styles.label}>Display name</Text>
            <TextInput
              style={styles.input}
              value={displayName}
              onChangeText={setDisplayName}
              placeholder="Evan"
              placeholderTextColor={theme.textDim}
            />
          </>
        ) : null}

        <Text style={styles.label}>Email</Text>
        <TextInput
          style={styles.input}
          value={email}
          onChangeText={setEmail}
          placeholder="you@example.com"
          placeholderTextColor={theme.textDim}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="email-address"
        />

        <Text style={styles.label}>Password</Text>
        <TextInput
          style={styles.input}
          value={password}
          onChangeText={setPassword}
          placeholder={mode === 'signup' ? 'At least 12 characters' : 'Your password'}
          placeholderTextColor={theme.textDim}
          secureTextEntry
        />

        {error ? <Text style={styles.error}>{error}</Text> : null}

        <Pressable style={[styles.primary, busy && styles.primaryBusy]} onPress={submit} disabled={busy}>
          {busy ? (
            <ActivityIndicator color="#fff" />
          ) : (
            <Text style={styles.primaryText}>{mode === 'login' ? 'Log in' : 'Create account'}</Text>
          )}
        </Pressable>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: theme.bg },
  scroll: { padding: 24, paddingTop: 64 },
  logo: { width: 84, height: 84, borderRadius: 20, alignSelf: 'center', marginBottom: 16 },
  title: { color: theme.text, fontSize: 30, fontWeight: '800', textAlign: 'center' },
  subtitle: { color: theme.textDim, fontSize: 14, textAlign: 'center', marginTop: 8, lineHeight: 20 },
  label: { color: theme.textDim, fontSize: 13, marginTop: 18, marginBottom: 6 },
  input: {
    backgroundColor: theme.bgInput,
    borderWidth: 1,
    borderColor: theme.border,
    borderRadius: 12,
    color: theme.text,
    fontSize: 16,
    paddingHorizontal: 14,
    paddingVertical: 12,
  },
  hint: { color: theme.textDim, fontSize: 12, marginTop: 6, lineHeight: 17 },
  tabs: { flexDirection: 'row', marginTop: 22, backgroundColor: theme.bgInput, borderRadius: 12, padding: 4 },
  tab: { flex: 1, paddingVertical: 10, borderRadius: 9, alignItems: 'center' },
  tabActive: { backgroundColor: '#2a2a44' },
  tabText: { color: theme.textDim, fontWeight: '600' },
  tabTextActive: { color: theme.text },
  error: { color: theme.danger, marginTop: 14, fontSize: 14, lineHeight: 20 },
  primary: {
    backgroundColor: theme.accent,
    borderRadius: 14,
    paddingVertical: 15,
    alignItems: 'center',
    marginTop: 22,
  },
  primaryBusy: { opacity: 0.7 },
  primaryText: { color: '#fff', fontSize: 16, fontWeight: '700' },
});
