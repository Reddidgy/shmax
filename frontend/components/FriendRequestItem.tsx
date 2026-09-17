import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet, Image } from 'react-native';

interface FriendRequestItemProps {
  request: {
    id: string;
    from_user?: { id: string; username: string; display_name: string; avatar_url?: string };
    to_user?: { id: string; username: string; display_name: string; avatar_url?: string };
    status: string;
    created_at: string;
  };
  type: 'incoming' | 'outgoing';
  onAccept?: (id: string) => void;
  onDecline?: (id: string) => void;
}

export default function FriendRequestItem({ request, type, onAccept, onDecline }: FriendRequestItemProps) {
  const user = type === 'incoming' ? request.from_user : request.to_user;
  const displayName = user?.display_name || user?.username || 'Unknown';
  const username = user?.username || '';

  return (
    <View style={styles.container}>
      <View style={styles.avatar}>
        {user?.avatar_url ? (
          <Image source={{ uri: user.avatar_url }} style={styles.avatarImage} />
        ) : (
          <View style={styles.avatarPlaceholder}>
            <Text style={styles.avatarText}>{displayName.charAt(0).toUpperCase()}</Text>
          </View>
        )}
      </View>
      <View style={styles.info}>
        <Text style={styles.displayName}>{displayName}</Text>
        {username ? <Text style={styles.username}>@{username}</Text> : null}
      </View>
      {type === 'incoming' && request.status === 'pending' ? (
        <View style={styles.actions}>
          <TouchableOpacity style={styles.acceptButton} onPress={() => onAccept?.(request.id)}>
            <Text style={styles.acceptText}>Accept</Text>
          </TouchableOpacity>
          <TouchableOpacity style={styles.declineButton} onPress={() => onDecline?.(request.id)}>
            <Text style={styles.declineText}>Decline</Text>
          </TouchableOpacity>
        </View>
      ) : (
        <View style={styles.statusBadge}>
          <Text style={styles.statusText}>
            {request.status === 'pending' ? 'Pending' : request.status}
          </Text>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flexDirection: 'row',
    alignItems: 'center',
    padding: 12,
    borderBottomWidth: 1,
    borderBottomColor: '#f0f0f0',
  },
  avatar: {
    marginRight: 12,
  },
  avatarImage: {
    width: 44,
    height: 44,
    borderRadius: 22,
  },
  avatarPlaceholder: {
    width: 44,
    height: 44,
    borderRadius: 22,
    backgroundColor: '#6C63FF',
    justifyContent: 'center',
    alignItems: 'center',
  },
  avatarText: {
    color: '#fff',
    fontSize: 18,
    fontWeight: '600',
  },
  info: {
    flex: 1,
  },
  displayName: {
    fontSize: 16,
    fontWeight: '600',
    color: '#1a1a1a',
  },
  username: {
    fontSize: 13,
    color: '#888',
    marginTop: 2,
  },
  actions: {
    flexDirection: 'row',
    gap: 8,
  },
  acceptButton: {
    backgroundColor: '#6C63FF',
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 8,
  },
  acceptText: {
    color: '#fff',
    fontWeight: '600',
    fontSize: 14,
  },
  declineButton: {
    backgroundColor: '#f0f0f0',
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 8,
  },
  declineText: {
    color: '#666',
    fontWeight: '600',
    fontSize: 14,
  },
  statusBadge: {
    backgroundColor: '#f0f0f0',
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 12,
  },
  statusText: {
    color: '#888',
    fontSize: 13,
    fontWeight: '500',
  },
});
