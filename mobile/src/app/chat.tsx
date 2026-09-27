/** Main chat screen: drawer + thread + composer, Grok/Gemini-style. */
import { Redirect, router } from 'expo-router';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  FlatList,
  Image,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  SafeAreaView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { ChatMsg, CortanaClient, ServerError } from '../api/client';
import { Composer } from '../components/Composer';
import { ConversationDrawer } from '../components/ConversationDrawer';
import { MessageBubble } from '../components/MessageBubble';
import {
  Conversation,
  getModelOverride,
  getTtsEnabled,
  loadConversations,
  newId,
  saveConversations,
  StoredMessage,
  titleFor,
} from '../store/conversations';
import { useSession } from '../store/sessionContext';
import { theme } from '../theme';
import { speak, stopSpeaking } from '../voice';

const SUGGESTIONS = [
  'Explain quantum computing simply',
  'Write a haiku about Indiana',
  'Help me plan my week',
  'What can you do?',
];

interface UiMessage extends StoredMessage {
  streaming?: boolean;
}

export default function ChatScreen() {
  const { session, client } = useSession();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [modelOverride, setModelOverride] = useState('');
  const [ttsOn, setTtsOn] = useState(false);
  const listRef = useRef<FlatList>(null);
  const cancelRef = useRef(false);
  /** Cancels the in-flight network request for real (stream abort / fetch abort). */
  const abortRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    (async () => {
      setConversations(await loadConversations());
      setModelOverride(await getModelOverride());
      setTtsOn(await getTtsEnabled());
    })();
  }, []);

  const persist = useCallback(async (list: Conversation[]) => {
    setConversations(list);
    await saveConversations(list);
  }, []);

  const active: Conversation | undefined = useMemo(
    () => conversations.find((c) => c.id === activeId),
    [conversations, activeId],
  );

  useEffect(() => {
    // Intentional reset: when the user opens a different conversation, the
    // visible thread is replaced wholesale. (Not derived state.)
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setMessages(
      (active?.messages ?? []).map((m) => ({ ...m })),
    );
    cancelRef.current = true; // stop any in-flight stream when switching chats
    abortRef.current?.();
    abortRef.current = null;
    setBusy(false);
  }, [activeId]); // eslint-disable-line react-hooks/exhaustive-deps

  const scrollToEnd = () => {
    requestAnimationFrame(() => listRef.current?.scrollToEnd({ animated: true }));
  };

  const newChat = useCallback(() => {
    cancelRef.current = true;
    abortRef.current?.();
    abortRef.current = null;
    stopSpeaking();
    setBusy(false);
    setActiveId(null);
    setMessages([]);
    setDrawerOpen(false);
  }, []);

  const deleteConversation = useCallback(
    async (id: string) => {
      const list = conversations.filter((c) => c.id !== id);
      if (activeId === id) {
        setActiveId(null);
        setMessages([]);
      }
      await persist(list);
    },
    [conversations, activeId, persist],
  );

  const send = useCallback(
    async (text: string) => {
      if (!client) {
        router.replace('/auth');
        return;
      }
      cancelRef.current = false;
      const userMsg: UiMessage = { role: 'user', content: text, ts: Date.now() };
      const assistantMsg: UiMessage = { role: 'assistant', content: '', ts: Date.now(), streaming: true };
      const next = [...messages, userMsg, assistantMsg];
      setMessages(next);
      setBusy(true);
      scrollToEnd();

      const apiMessages: ChatMsg[] = next
        .filter((m) => !m.streaming || m.content)
        .map((m) => ({ role: m.role, content: m.content }));

      let convId = activeId;
      let convTitle = active?.title;
      if (!convId) {
        convId = newId();
        convTitle = titleFor(text);
      }

      /** One plain-JSON retry when streaming yielded nothing usable. */
      const chatOnceFallback = async () => {
        const ctrl = new AbortController();
        abortRef.current = () => ctrl.abort();
        const fallbackTimer = setTimeout(() => ctrl.abort(), 120000);
        try {
          const full = await (client as CortanaClient).chatOnce(
            apiMessages,
            modelOverride || undefined,
            ctrl.signal,
          );
          if (!cancelRef.current) {
            assistantMsg.content = full;
            setMessages((prev) =>
              prev.map((m) => (m === assistantMsg ? { ...m, content: full } : m)),
            );
          }
        } finally {
          clearTimeout(fallbackTimer);
          abortRef.current = null;
        }
      };

      // Stream when supported…
      const stream = (client as CortanaClient).streamChat({
        messages: apiMessages,
        model: modelOverride || undefined,
        onToken: (tok) => {
          if (cancelRef.current) return;
          assistantMsg.content += tok;
          setMessages((prev) =>
            prev.map((m) => (m === assistantMsg ? { ...m, content: assistantMsg.content } : m)),
          );
        },
      });
      abortRef.current = () => stream.abort();
      let streamError: unknown = null;
      try {
        await stream.promise;
      } catch (e) {
        streamError = e;
      } finally {
        abortRef.current = null;
      }

      // …otherwise fall back to a single plain-JSON completion. This covers
      // transports that can't do SSE and servers that answer 200 without any
      // usable delta. It never masks real HTTP errors (4xx/5xx) and never
      // fires after the user stopped the request.
      const showStreamError = () => {
        const msg = streamError instanceof Error ? streamError.message : 'Something went wrong.';
        assistantMsg.content = assistantMsg.content || `⚠️ ${msg}`;
        setMessages((prev) =>
          prev.map((m) => (m === assistantMsg ? { ...m, content: assistantMsg.content } : m)),
        );
      };
      try {
        if (!cancelRef.current && assistantMsg.content === '' && streamError === null) {
          await chatOnceFallback();
        } else if (
          !cancelRef.current &&
          assistantMsg.content === '' &&
          streamError instanceof ServerError &&
          streamError.status === 0
        ) {
          // Transport died before any token arrived — one plain-JSON retry.
          await chatOnceFallback();
        } else if (!cancelRef.current && streamError) {
          showStreamError();
        }
      } catch (e) {
        if (!cancelRef.current) {
          const msg =
            e instanceof Error && e.name === 'AbortError'
              ? 'Chat request timed out.'
              : e instanceof Error
                ? e.message
                : 'Something went wrong.';
          assistantMsg.content = assistantMsg.content || `⚠️ ${msg}`;
          setMessages((prev) =>
            prev.map((m) => (m === assistantMsg ? { ...m, content: assistantMsg.content } : m)),
          );
        }
      } finally {
        if (!cancelRef.current) {
          assistantMsg.streaming = false;
          const finalMessages: StoredMessage[] = [...next].map(({ streaming: _s, ...m }) => m);
          setMessages(finalMessages.map((m) => ({ ...m })));
          const updated: Conversation = {
            id: convId as string,
            title: convTitle as string,
            messages: finalMessages,
            updatedAt: Date.now(),
          };
          const list = [updated, ...conversations.filter((c) => c.id !== convId)];
          await persist(list);
          setActiveId(convId);
          if (ttsOn && assistantMsg.content && !assistantMsg.content.startsWith('⚠️')) {
            speak(assistantMsg.content);
          }
        }
        setBusy(false);
        scrollToEnd();
      }
    },
    [client, messages, activeId, active, conversations, persist, modelOverride, ttsOn],
  );

  const stop = useCallback(() => {
    cancelRef.current = true;
    abortRef.current?.(); // actually cancel the in-flight request
    abortRef.current = null;
    stopSpeaking();
    setBusy(false);
    setMessages((prev) => prev.map((m) => ({ ...m, streaming: false })));
  }, []);

  if (!session.token) {
    return <Redirect href="/auth" />;
  }

  const modelLabel = modelOverride || 'server default';

  return (
    <SafeAreaView style={styles.root}>
      <KeyboardAvoidingView
        style={styles.root}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        keyboardVerticalOffset={0}
      >
        {/* header */}
        <View style={styles.header}>
          <Pressable onPress={() => setDrawerOpen(true)} hitSlop={12} accessibilityLabel="Conversations">
            <Text style={styles.headerIcon}>☰</Text>
          </Pressable>
          <View style={styles.headerCenter}>
            <Text style={styles.headerTitle}>Cortana</Text>
            <Text style={styles.headerSub} numberOfLines={1}>
              {modelLabel}
            </Text>
          </View>
          <Pressable onPress={newChat} hitSlop={12} accessibilityLabel="New chat">
            <Text style={styles.headerIcon}>✎</Text>
          </Pressable>
          <Pressable
            onPress={() => router.push('/settings')}
            hitSlop={12}
            accessibilityLabel="Settings"
          >
            <Text style={styles.headerIcon}>⚙</Text>
          </Pressable>
        </View>

        {messages.length === 0 ? (
          <View style={styles.empty}>
            <Image source={require('../../assets/icon.png')} style={styles.emptyLogo} />
            <Text style={styles.emptyTitle}>How can I help?</Text>
            <Text style={styles.emptySub}>
              Replies come from your Cortana server —{'\n'}this app is just the window.
            </Text>
            <View style={styles.chips}>
              {SUGGESTIONS.map((s) => (
                <Pressable key={s} style={styles.chip} onPress={() => send(s)}>
                  <Text style={styles.chipText}>{s}</Text>
                </Pressable>
              ))}
            </View>
          </View>
        ) : (
          <FlatList
            ref={listRef}
            data={messages}
            keyExtractor={(_, i) => `${i}`}
            style={styles.list}
            contentContainerStyle={styles.listContent}
            onContentSizeChange={scrollToEnd}
            renderItem={({ item }) => (
              <MessageBubble role={item.role} content={item.content} streaming={item.streaming} />
            )}
          />
        )}

        <Composer onSend={send} busy={busy} onStop={stop} />
      </KeyboardAvoidingView>

      <ConversationDrawer
        visible={drawerOpen}
        conversations={conversations}
        activeId={activeId}
        onClose={() => setDrawerOpen(false)}
        onSelect={setActiveId}
        onNew={newChat}
        onDelete={deleteConversation}
        userLabel={session.user ? `${session.user.display_name} · ${session.user.email}` : ''}
      />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: theme.bg },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 14,
    paddingVertical: 10,
    borderBottomWidth: 1,
    borderBottomColor: theme.border,
    gap: 14,
  },
  headerIcon: { color: theme.text, fontSize: 22 },
  headerCenter: { flex: 1, alignItems: 'center' },
  headerTitle: { color: theme.text, fontSize: 16, fontWeight: '700' },
  headerSub: { color: theme.textDim, fontSize: 11, marginTop: 1 },
  list: { flex: 1 },
  listContent: { paddingVertical: 8 },
  empty: { flex: 1, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 28 },
  emptyLogo: { width: 76, height: 76, borderRadius: 18, marginBottom: 18 },
  emptyTitle: { color: theme.text, fontSize: 24, fontWeight: '700', marginBottom: 8 },
  emptySub: { color: theme.textDim, fontSize: 13, textAlign: 'center', lineHeight: 19, marginBottom: 22 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', justifyContent: 'center', gap: 8 },
  chip: {
    borderWidth: 1,
    borderColor: theme.border,
    backgroundColor: theme.bgElevated,
    borderRadius: 18,
    paddingHorizontal: 14,
    paddingVertical: 9,
  },
  chipText: { color: theme.text, fontSize: 13 },
});
