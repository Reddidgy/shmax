import { View, Text, TouchableOpacity, StyleSheet, Platform } from 'react-native';
import { useCallStore } from '../store/callStore';

export default function IncomingCallOverlay() {
  const { callState, caller, acceptCall, declineCall } = useCallStore();

  if (callState !== 'incoming' || !caller) {
    return null;
  }

  return (
    <View style={styles.overlay}>
      <View style={styles.card}>
        <View style={styles.callerInfo}>
          <View style={styles.avatar}>
            <Text style={styles.avatarText}>
              {caller.display_name[0]?.toUpperCase()}
            </Text>
          </View>
          <Text style={styles.callerName}>{caller.display_name}</Text>
          <Text style={styles.callLabel}>Incoming Video Call</Text>
        </View>

        <View style={styles.actions}>
          <TouchableOpacity
            style={[styles.actionButton, styles.declineButton]}
            onPress={declineCall}
          >
            <Text style={styles.actionIcon}>✕</Text>
            <Text style={[styles.actionLabel, { color: '#FF3B30' }]}>
              Decline
            </Text>
          </TouchableOpacity>

          <TouchableOpacity
            style={[styles.actionButton, styles.acceptButton]}
            onPress={acceptCall}
          >
            <Text style={styles.actionIcon}>📞</Text>
            <Text style={[styles.actionLabel, { color: '#4CD964' }]}>
              Accept
            </Text>
          </TouchableOpacity>
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  overlay: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: 'rgba(0,0,0,0.6)',
    justifyContent: 'center',
    alignItems: 'center',
    zIndex: 999,
  },
  card: {
    backgroundColor: '#fff',
    borderRadius: 24,
    padding: 32,
    width: 300,
    alignItems: 'center',
    ...(Platform.OS === 'web'
      ? { boxShadow: '0 8px 32px rgba(0,0,0,0.3)' }
      : {
          shadowColor: '#000',
          shadowOffset: { width: 0, height: 8 },
          shadowOpacity: 0.3,
          shadowRadius: 16,
          elevation: 10,
        }),
  },
  callerInfo: {
    alignItems: 'center',
    marginBottom: 32,
  },
  avatar: {
    width: 80,
    height: 80,
    borderRadius: 40,
    backgroundColor: '#007AFF',
    justifyContent: 'center',
    alignItems: 'center',
    marginBottom: 16,
  },
  avatarText: {
    color: '#fff',
    fontSize: 32,
    fontWeight: 'bold',
  },
  callerName: {
    fontSize: 22,
    fontWeight: '700',
    color: '#000',
    marginBottom: 4,
  },
  callLabel: {
    fontSize: 15,
    color: '#666',
  },
  actions: {
    flexDirection: 'row',
    gap: 40,
  },
  actionButton: {
    width: 64,
    height: 64,
    borderRadius: 32,
    justifyContent: 'center',
    alignItems: 'center',
  },
  declineButton: {
    backgroundColor: '#FFE5E5',
  },
  acceptButton: {
    backgroundColor: '#E5FFE9',
  },
  actionIcon: {
    fontSize: 24,
  },
  actionLabel: {
    fontSize: 12,
    marginTop: 4,
    fontWeight: '600',
  },
});
