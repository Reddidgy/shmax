import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';

interface ContactItemProps {
  contact: {
    display_name: string;
    username: string;
  };
  onPress: () => void;
  isOnline?: boolean;
}

export default function ContactItem({
  contact,
  onPress,
  isOnline = false,
}: ContactItemProps) {
  return (
    <TouchableOpacity style={styles.container} onPress={onPress}>
      <View style={styles.avatarContainer}>
        <View style={styles.avatar}>
          <Text style={styles.avatarText}>
            {contact.display_name[0]?.toUpperCase()}
          </Text>
        </View>
        {isOnline && <View style={styles.onlineIndicator} />}
      </View>
      <View style={styles.info}>
        <Text style={styles.displayName}>{contact.display_name}</Text>
        <Text style={styles.username}>@{contact.username}</Text>
      </View>
    </TouchableOpacity>
  );
}

const styles = StyleSheet.create({
  container: {
    flexDirection: 'row',
    alignItems: 'center',
    padding: 16,
    borderBottomWidth: 1,
    borderBottomColor: '#f0f0f0',
  },
  avatarContainer: {
    position: 'relative',
    marginRight: 12,
  },
  avatar: {
    width: 48,
    height: 48,
    borderRadius: 24,
    backgroundColor: '#007AFF',
    justifyContent: 'center',
    alignItems: 'center',
  },
  avatarText: {
    color: '#fff',
    fontSize: 20,
    fontWeight: 'bold',
  },
  onlineIndicator: {
    position: 'absolute',
    bottom: 0,
    right: 0,
    width: 14,
    height: 14,
    borderRadius: 7,
    backgroundColor: '#34C759',
    borderWidth: 2,
    borderColor: '#fff',
  },
  info: {
    flex: 1,
  },
  displayName: {
    fontSize: 17,
    fontWeight: '600',
    marginBottom: 2,
  },
  username: {
    fontSize: 14,
    color: '#666',
  },
});
