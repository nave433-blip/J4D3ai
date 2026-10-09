/** Slide-in conversation list drawer (Grok/Gemini-style). */
import React from 'react';
import {
  FlatList,
  Modal,
  Pressable,
  SafeAreaView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import type { Conversation } from '../store/conversations';
import { theme } from '../theme';

interface Props {
  visible: boolean;
  conversations: Conversation[];
  activeId: string | null;
  onClose: () => void;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  userLabel: string;
}

export function ConversationDrawer({
  visible,
  conversations,
  activeId,
  onClose,
  onSelect,
  onNew,
  onDelete,
  userLabel,
}: Props) {
  return (
    <Modal visible={visible} animationType="slide" transparent onRequestClose={onClose}>
      <View style={styles.overlay}>
        <Pressable style={styles.scrim} onPress={onClose} />
        <SafeAreaView style={styles.panel}>
          <View style={styles.header}>
            <Text style={styles.title}>Jade</Text>
            <Pressable onPress={onClose} hitSlop={12} accessibilityLabel="Close">
              <Text style={styles.close}>✕</Text>
            </Pressable>
          </View>
          <Pressable style={styles.newBtn} onPress={onNew}>
            <Text style={styles.newBtnText}>＋ New chat</Text>
          </Pressable>
          <FlatList
            data={conversations}
            keyExtractor={(c) => c.id}
            style={styles.list}
            ListEmptyComponent={<Text style={styles.empty}>No conversations yet.</Text>}
            renderItem={({ item }) => (
              <View style={[styles.item, item.id === activeId && styles.itemActive]}>
                <Pressable
                  style={styles.itemPress}
                  onPress={() => {
                    onSelect(item.id);
                    onClose();
                  }}
                >
                  <Text style={styles.itemTitle} numberOfLines={1}>
                    {item.title}
                  </Text>
                  <Text style={styles.itemMeta} numberOfLines={1}>
                    {item.messages.length} messages ·{' '}
                    {new Date(item.updatedAt).toLocaleDateString()}
                  </Text>
                </Pressable>
                <Pressable
                  onPress={() => onDelete(item.id)}
                  hitSlop={10}
                  accessibilityLabel="Delete conversation"
                >
                  <Text style={styles.delete}>🗑</Text>
                </Pressable>
              </View>
            )}
          />
          <View style={styles.footer}>
            <Text style={styles.userLabel} numberOfLines={1}>
              {userLabel}
            </Text>
          </View>
        </SafeAreaView>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  overlay: { flex: 1, flexDirection: 'row' },
  scrim: { flex: 1, backgroundColor: 'rgba(0,0,0,0.5)' },
  panel: { width: '80%', maxWidth: 340, backgroundColor: theme.bgElevated, height: '100%' },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingVertical: 14,
    borderBottomWidth: 1,
    borderBottomColor: theme.border,
  },
  title: { color: theme.text, fontSize: 18, fontWeight: '700' },
  close: { color: theme.textDim, fontSize: 18 },
  newBtn: {
    margin: 12,
    backgroundColor: theme.bgInput,
    borderRadius: 12,
    paddingVertical: 12,
    alignItems: 'center',
    borderWidth: 1,
    borderColor: theme.border,
  },
  newBtnText: { color: theme.text, fontSize: 15, fontWeight: '600' },
  list: { flex: 1, paddingHorizontal: 12 },
  empty: { color: theme.textDim, textAlign: 'center', marginTop: 24 },
  item: {
    flexDirection: 'row',
    alignItems: 'center',
    borderRadius: 10,
    paddingHorizontal: 10,
    paddingVertical: 10,
    marginBottom: 4,
  },
  itemActive: { backgroundColor: '#1e1e33' },
  itemPress: { flex: 1 },
  itemTitle: { color: theme.text, fontSize: 14, fontWeight: '600' },
  itemMeta: { color: theme.textDim, fontSize: 12, marginTop: 2 },
  delete: { fontSize: 16, paddingLeft: 8 },
  footer: { padding: 16, borderTopWidth: 1, borderTopColor: theme.border },
  userLabel: { color: theme.textDim, fontSize: 13 },
});
