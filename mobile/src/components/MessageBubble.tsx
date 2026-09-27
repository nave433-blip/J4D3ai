/** One chat message bubble with markdown rendering + read-aloud button. */
import React, { useMemo } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import Markdown from 'react-native-markdown-display';
import { theme } from '../theme';
import { speak, stopSpeaking } from '../voice';

interface Props {
  role: 'user' | 'assistant';
  content: string;
  streaming?: boolean;
}

const markdownStyles = StyleSheet.create({
  body: { color: theme.text, fontSize: 15, lineHeight: 22 },
  heading1: { color: theme.text, fontSize: 20, fontWeight: '700', marginVertical: 6 },
  heading2: { color: theme.text, fontSize: 18, fontWeight: '700', marginVertical: 6 },
  heading3: { color: theme.text, fontSize: 16, fontWeight: '700', marginVertical: 4 },
  code_inline: {
    color: '#c9b8ff',
    backgroundColor: '#1e1e30',
    fontFamily: theme.fontMono,
    fontSize: 13,
    paddingHorizontal: 4,
    borderRadius: 4,
  },
  fence: {
    backgroundColor: '#14141f',
    borderColor: theme.border,
    borderWidth: 1,
    borderRadius: 8,
    padding: 10,
    marginVertical: 6,
  },
  code_block: { color: '#d8d8ea', fontFamily: theme.fontMono, fontSize: 13 },
  bullet_list: { marginVertical: 4 },
  ordered_list: { marginVertical: 4 },
  list_item: { flexDirection: 'row', marginVertical: 2 },
  bullet_list_icon: { color: theme.accent, marginRight: 8 },
  ordered_list_icon: { color: theme.accent, marginRight: 8 },
  link: { color: theme.accent },
  blockquote: {
    backgroundColor: '#14141f',
    borderLeftColor: theme.accent,
    borderLeftWidth: 3,
    paddingHorizontal: 10,
    paddingVertical: 6,
    marginVertical: 6,
  },
  hr: { backgroundColor: theme.border, height: 1, marginVertical: 8 },
  table: { borderColor: theme.border, borderWidth: 1, borderRadius: 6, marginVertical: 6 },
  th: { color: theme.text, fontWeight: '700', padding: 6 },
  td: { color: theme.text, padding: 6 },
  tr: { borderBottomWidth: 1, borderColor: theme.border },
});

export function MessageBubble({ role, content, streaming }: Props) {
  const isUser = role === 'user';

  const body = useMemo(() => {
    if (isUser) {
      return <Text style={styles.userText}>{content}</Text>;
    }
    if (!content && streaming) {
      return (
        <View style={styles.thinking}>
          <ActivityIndicator size="small" color={theme.accent} />
          <Text style={styles.thinkingText}>Thinking…</Text>
        </View>
      );
    }
    return <Markdown style={markdownStyles}>{content}</Markdown>;
  }, [content, isUser, streaming]);

  return (
    <View style={[styles.row, isUser ? styles.rowRight : styles.rowLeft]}>
      <View style={[styles.bubble, isUser ? styles.userBubble : styles.assistantBubble]}>
        {body}
        {!isUser && !!content && (
          <View style={styles.actions}>
            <Pressable
              onPress={() => speak(content)}
              hitSlop={10}
              accessibilityLabel="Read reply aloud"
              style={styles.actionBtn}
            >
              <Text style={styles.actionText}>🔊 Read aloud</Text>
            </Pressable>
            <Pressable
              onPress={stopSpeaking}
              hitSlop={10}
              accessibilityLabel="Stop reading"
              style={styles.actionBtn}
            >
              <Text style={styles.actionText}>⏹ Stop</Text>
            </Pressable>
            {streaming ? <Text style={styles.streamingDot}>●</Text> : null}
          </View>
        )}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', marginVertical: 6, paddingHorizontal: 12 },
  rowRight: { justifyContent: 'flex-end' },
  rowLeft: { justifyContent: 'flex-start' },
  bubble: { maxWidth: '88%', borderRadius: theme.radius, paddingHorizontal: 14, paddingVertical: 10 },
  userBubble: { backgroundColor: theme.userBubble },
  assistantBubble: { backgroundColor: 'transparent', paddingHorizontal: 2, maxWidth: '94%' },
  userText: { color: theme.text, fontSize: 15, lineHeight: 22 },
  thinking: { flexDirection: 'row', alignItems: 'center', gap: 8, paddingVertical: 4 },
  thinkingText: { color: theme.textDim, fontSize: 14 },
  actions: { flexDirection: 'row', alignItems: 'center', marginTop: 6, gap: 12 },
  actionBtn: { paddingVertical: 4 },
  actionText: { color: theme.textDim, fontSize: 12 },
  streamingDot: { color: theme.accent, fontSize: 10 },
});
