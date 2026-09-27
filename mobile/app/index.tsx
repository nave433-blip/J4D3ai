/** Boot gate: decide auth vs chat once the stored session is validated. */
import { Redirect } from 'expo-router';
import { ActivityIndicator, StyleSheet, View } from 'react-native';
import { useSession } from '../src/store/sessionContext';
import { theme } from '../src/theme';

export default function Index() {
  const { ready, session } = useSession();

  if (!ready) {
    return (
      <View style={styles.center}>
        <ActivityIndicator size="large" color={theme.accent} />
      </View>
    );
  }
  if (session.token && session.user) return <Redirect href="/chat" />;
  return <Redirect href="/auth" />;
}

const styles = StyleSheet.create({
  center: { flex: 1, backgroundColor: theme.bg, alignItems: 'center', justifyContent: 'center' },
});
