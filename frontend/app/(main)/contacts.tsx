import { useEffect, useState } from 'react';
import {
  View,
  Text,
  FlatList,
  TouchableOpacity,
  StyleSheet,
  TextInput,
  Alert,
  ActivityIndicator,
  Clipboard,
} from 'react-native';
import { useRouter } from 'expo-router';
import { useContactStore } from '../../store/contactStore';
import { useChatStore } from '../../store/chatStore';
import { useFriendRequestStore } from '../../store/friendRequestStore';
import FriendRequestItem from '../../components/FriendRequestItem';

export default function ContactsScreen() {
  const router = useRouter();
  const [searchQuery, setSearchQuery] = useState('');
  const [isSearchMode, setIsSearchMode] = useState(false);
  const {
    contacts,
    searchResults,
    isLoading,
    fetchContacts,
    searchUsers,
    addContact,
    removeContact,
    clearSearch,
  } = useContactStore();
  const { createConversation } = useChatStore();
  const {
    incomingRequests,
    inviteLink,
    fetchIncoming,
    acceptRequest,
    declineRequest,
    generateInviteLink,
  } = useFriendRequestStore();

  useEffect(() => {
    fetchContacts();
    fetchIncoming();
  }, []);

  useEffect(() => {
    const delayDebounceFn = setTimeout(() => {
      if (searchQuery.trim().length > 0) {
        setIsSearchMode(true);
        searchUsers(searchQuery);
      } else {
        setIsSearchMode(false);
        clearSearch();
      }
    }, 300);

    return () => clearTimeout(delayDebounceFn);
  }, [searchQuery]);

  const handleAddContact = async (userId: string) => {
    try {
      await addContact(userId);
      Alert.alert('Success', 'Contact added');
      setSearchQuery('');
      setIsSearchMode(false);
    } catch (error) {
      Alert.alert('Error', 'Failed to add contact');
    }
  };

  const handleRemoveContact = async (contactId: string) => {
    Alert.alert('Remove Contact', 'Are you sure?', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Remove',
        style: 'destructive',
        onPress: async () => {
          try {
            await removeContact(contactId);
          } catch (error) {
            Alert.alert('Error', 'Failed to remove contact');
          }
        },
      },
    ]);
  };

  const handleStartChat = async (userId: string) => {
    try {
      const conversationId = await createConversation([userId]);
      router.push(`/conversation/${conversationId}`);
    } catch (error) {
      Alert.alert('Error', 'Failed to start conversation');
    }
  };

  const handleGenerateInviteLink = async () => {
    try {
      await generateInviteLink();
      Alert.alert('Success', 'Invite link generated');
    } catch (error) {
      Alert.alert('Error', 'Failed to generate invite link');
    }
  };

  const handleCopyInviteCode = () => {
    if (inviteLink?.code) {
      Clipboard.setString(inviteLink.code);
      Alert.alert('Copied', 'Invite code copied to clipboard');
    }
  };

  const handleAcceptRequest = async (requestId: string) => {
    try {
      await acceptRequest(requestId);
      await fetchContacts();
      Alert.alert('Success', 'Friend request accepted');
    } catch (error) {
      Alert.alert('Error', 'Failed to accept friend request');
    }
  };

  const handleDeclineRequest = async (requestId: string) => {
    try {
      await declineRequest(requestId);
      Alert.alert('Success', 'Friend request declined');
    } catch (error) {
      Alert.alert('Error', 'Failed to decline friend request');
    }
  };

  const renderContact = ({ item }: any) => (
    <TouchableOpacity
      style={styles.contactItem}
      onPress={() => handleStartChat(item.user.id)}
      onLongPress={() => handleRemoveContact(item.id)}
    >
      <View style={styles.avatar}>
        <Text style={styles.avatarText}>
          {item.user.display_name[0]?.toUpperCase()}
        </Text>
      </View>
      <View style={styles.contactInfo}>
        <Text style={styles.contactName}>{item.user.display_name}</Text>
        <Text style={styles.contactUsername}>@{item.user.username}</Text>
      </View>
    </TouchableOpacity>
  );

  const renderSearchResult = ({ item }: any) => (
    <TouchableOpacity
      style={styles.contactItem}
      onPress={() => handleAddContact(item.id)}
    >
      <View style={styles.avatar}>
        <Text style={styles.avatarText}>
          {item.display_name[0]?.toUpperCase()}
        </Text>
      </View>
      <View style={styles.contactInfo}>
        <Text style={styles.contactName}>{item.display_name}</Text>
        <Text style={styles.contactUsername}>@{item.username}</Text>
      </View>
      <View style={styles.addButton}>
        <Text style={styles.addButtonText}>Add</Text>
      </View>
    </TouchableOpacity>
  );

  return (
    <View style={styles.container}>
      <View style={styles.searchContainer}>
        <TextInput
          style={styles.searchInput}
          placeholder="Search users..."
          value={searchQuery}
          onChangeText={setSearchQuery}
          autoCapitalize="none"
          autoCorrect={false}
        />
      </View>

      {incomingRequests.length > 0 && !isSearchMode && (
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Friend Requests</Text>
          {incomingRequests.map((request) => (
            <FriendRequestItem
              key={request.id}
              request={request}
              type="incoming"
              onAccept={handleAcceptRequest}
              onDecline={handleDeclineRequest}
            />
          ))}
        </View>
      )}

      {!isSearchMode && (
        <View style={styles.inviteSection}>
          <TouchableOpacity
            style={styles.inviteButton}
            onPress={handleGenerateInviteLink}
          >
            <Text style={styles.inviteButtonText}>Generate Invite Link</Text>
          </TouchableOpacity>
          {inviteLink && (
            <View style={styles.inviteLinkContainer}>
              <Text style={styles.inviteLinkLabel}>Invite Code:</Text>
              <TouchableOpacity onPress={handleCopyInviteCode}>
                <Text style={styles.inviteLinkCode}>{inviteLink.code}</Text>
              </TouchableOpacity>
              <Text style={styles.inviteLinkExpiry}>
                Expires: {new Date(inviteLink.expires_at).toLocaleDateString()}
              </Text>
            </View>
          )}
        </View>
      )}

      {isLoading && (
        <View style={styles.loadingContainer}>
          <ActivityIndicator size="small" color="#007AFF" />
        </View>
      )}

      <FlatList
        data={isSearchMode ? searchResults : contacts}
        keyExtractor={(item) => item.id}
        renderItem={isSearchMode ? renderSearchResult : renderContact}
        ListEmptyComponent={
          <View style={styles.emptyContainer}>
            <Text style={styles.emptyText}>
              {isSearchMode ? 'No users found' : 'No contacts yet'}
            </Text>
            <Text style={styles.emptySubtext}>
              {isSearchMode
                ? 'Try a different search term'
                : 'Search to add contacts'}
            </Text>
          </View>
        }
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#fff',
  },
  searchContainer: {
    padding: 16,
    borderBottomWidth: 1,
    borderBottomColor: '#f0f0f0',
  },
  searchInput: {
    height: 40,
    borderWidth: 1,
    borderColor: '#ddd',
    borderRadius: 8,
    paddingHorizontal: 12,
    fontSize: 16,
  },
  loadingContainer: {
    padding: 8,
    alignItems: 'center',
  },
  contactItem: {
    flexDirection: 'row',
    alignItems: 'center',
    padding: 16,
    borderBottomWidth: 1,
    borderBottomColor: '#f0f0f0',
  },
  avatar: {
    width: 48,
    height: 48,
    borderRadius: 24,
    backgroundColor: '#007AFF',
    justifyContent: 'center',
    alignItems: 'center',
    marginRight: 12,
  },
  avatarText: {
    color: '#fff',
    fontSize: 20,
    fontWeight: 'bold',
  },
  contactInfo: {
    flex: 1,
  },
  contactName: {
    fontSize: 17,
    fontWeight: '600',
    marginBottom: 2,
  },
  contactUsername: {
    fontSize: 14,
    color: '#666',
  },
  addButton: {
    backgroundColor: '#007AFF',
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 16,
  },
  addButtonText: {
    color: '#fff',
    fontSize: 14,
    fontWeight: '600',
  },
  emptyContainer: {
    padding: 40,
    alignItems: 'center',
  },
  emptyText: {
    fontSize: 18,
    fontWeight: '600',
    color: '#666',
    marginBottom: 8,
  },
  emptySubtext: {
    fontSize: 14,
    color: '#999',
  },
  section: {
    backgroundColor: '#fff',
    borderBottomWidth: 1,
    borderBottomColor: '#f0f0f0',
  },
  sectionTitle: {
    fontSize: 18,
    fontWeight: '700',
    paddingHorizontal: 16,
    paddingVertical: 12,
    backgroundColor: '#f9f9f9',
    color: '#333',
  },
  inviteSection: {
    padding: 16,
    backgroundColor: '#f9f9f9',
    borderBottomWidth: 1,
    borderBottomColor: '#f0f0f0',
  },
  inviteButton: {
    backgroundColor: '#007AFF',
    paddingVertical: 12,
    paddingHorizontal: 16,
    borderRadius: 8,
    alignItems: 'center',
  },
  inviteButtonText: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '600',
  },
  inviteLinkContainer: {
    marginTop: 12,
    padding: 12,
    backgroundColor: '#fff',
    borderRadius: 8,
    borderWidth: 1,
    borderColor: '#ddd',
  },
  inviteLinkLabel: {
    fontSize: 14,
    color: '#666',
    marginBottom: 6,
  },
  inviteLinkCode: {
    fontSize: 16,
    fontWeight: '600',
    color: '#007AFF',
    marginBottom: 6,
  },
  inviteLinkExpiry: {
    fontSize: 12,
    color: '#999',
  },
});
