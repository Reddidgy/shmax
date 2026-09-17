import { useEffect, useState, useRef } from 'react';
import {
  View,
  Text,
  FlatList,
  TextInput,
  TouchableOpacity,
  StyleSheet,
  KeyboardAvoidingView,
  Platform,
  ActivityIndicator,
  AppState,
} from 'react-native';
import { useLocalSearchParams, Stack } from 'expo-router';
import { useChatStore } from '../../store/chatStore';
import { useAuthStore } from '../../store/authStore';
import MessageBubble from '../../components/MessageBubble';
import VideoCircleRecorder from '../../components/VideoCircleRecorder';
import websocketClient from '../../services/websocket';
import api from '../../services/api';
import { useCallStore } from '../../store/callStore';

export default function ConversationScreen() {
  const { id } = useLocalSearchParams();
  const conversationId = id as string;
  const [messageText, setMessageText] = useState('');
  const [isTyping, setIsTyping] = useState(false);
  const [showRecorder, setShowRecorder] = useState(false);
  const flatListRef = useRef<FlatList>(null);
  const typingTimeoutRef = useRef<NodeJS.Timeout>();

  const { messages, isLoading, fetchMessages, sendMessage, typingUsers } =
    useChatStore();
  const { user } = useAuthStore();
  const { initiateCall, callState } = useCallStore();
  const [conversationParticipants, setConversationParticipants] = useState<any[]>([]);

  useEffect(() => {
    const loadParticipants = async () => {
      try {
        const response = await api.conversations.getById(conversationId);
        setConversationParticipants(response.data.participants || []);
      } catch (error) {
        console.error('Failed to load conversation:', error);
      }
    };
    loadParticipants();
  }, [conversationId]);

  const handleVideoCall = () => {
    const otherParticipant = conversationParticipants.find(
      (p: any) => p.id !== user?.id
    );
    if (otherParticipant && callState === 'idle') {
      initiateCall({
        id: otherParticipant.id,
        display_name: otherParticipant.display_name,
        avatar_url: otherParticipant.avatar_url,
      });
    }
  };

  const conversationMessages = messages[conversationId] || [];
  const typingUserIds = typingUsers[conversationId] || new Set();

  useEffect(() => {
    fetchMessages(conversationId);
  }, [conversationId]);

  useEffect(() => {
    const unsubscribe = websocketClient.onReconnect(() => {
      fetchMessages(conversationId);
    });
    return unsubscribe;
  }, [conversationId]);

  useEffect(() => {
    const sub = AppState.addEventListener('change', (state) => {
      if (state === 'active') {
        fetchMessages(conversationId);
      }
    });
    return () => sub.remove();
  }, [conversationId]);

  useEffect(() => {
    if (conversationMessages.length > 0) {
      setTimeout(() => {
        flatListRef.current?.scrollToEnd({ animated: true });
      }, 100);
    }
  }, [conversationMessages.length]);

  const handleSendMessage = async () => {
    if (!messageText.trim()) return;

    const text = messageText;
    setMessageText('');

    // Stop typing indicator
    if (isTyping) {
      websocketClient.sendTypingStop(conversationId);
      setIsTyping(false);
    }

    try {
      await sendMessage(conversationId, text);
    } catch (error) {
      console.error('Failed to send message:', error);
    }
  };

  const handleTextChange = (text: string) => {
    setMessageText(text);

    if (text.trim() && !isTyping) {
      websocketClient.sendTypingStart(conversationId);
      setIsTyping(true);
    }

    if (typingTimeoutRef.current) {
      clearTimeout(typingTimeoutRef.current);
    }

    typingTimeoutRef.current = setTimeout(() => {
      if (isTyping) {
        websocketClient.sendTypingStop(conversationId);
        setIsTyping(false);
      }
    }, 2000);
  };

  const handleSendVideoCircle = async (videoUri: string) => {
    setShowRecorder(false);
    try {
      // Upload the video file
      const formData = new FormData();

      // Handle file URI for React Native vs web
      if (Platform.OS === 'web') {
        const response = await fetch(videoUri);
        const blob = await response.blob();
        formData.append('file', blob, 'video_circle.mp4');
      } else {
        formData.append('file', {
          uri: videoUri,
          type: 'video/mp4',
          name: 'video_circle.mp4',
        } as any);
      }

      const uploadResponse = await api.instance.post('/media/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });

      const mediaUrl = uploadResponse.data.url;

      // Send the message
      await api.conversations.sendMessage(conversationId, {
        message_type: 'video_circle',
        media_url: mediaUrl,
        content: null,
      });

      // Refresh messages
      await fetchMessages(conversationId);
    } catch (error) {
      console.error('Failed to send video circle:', error);
    }
  };

  const renderMessage = ({ item }: any) => (
    <MessageBubble
      message={item}
      isOwn={item.sender.id === user?.id}
      showSender={true}
    />
  );

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      keyboardVerticalOffset={100}
    >
      <Stack.Screen
        options={{
          headerShown: true,
          headerTitle: 'Chat',
          headerBackTitle: 'Back',
          headerRight: () => (
            <TouchableOpacity
              onPress={handleVideoCall}
              style={{ paddingHorizontal: 12, paddingVertical: 8 }}
              disabled={callState !== 'idle'}
            >
              <Text style={{ fontSize: 22, opacity: callState !== 'idle' ? 0.4 : 1 }}>📹</Text>
            </TouchableOpacity>
          ),
        }}
      />

      {isLoading && conversationMessages.length === 0 ? (
        <View style={styles.loadingContainer}>
          <ActivityIndicator size="large" color="#007AFF" />
        </View>
      ) : (
        <FlatList
          ref={flatListRef}
          data={conversationMessages}
          keyExtractor={(item) => item.id}
          renderItem={renderMessage}
          contentContainerStyle={styles.messagesList}
          ListEmptyComponent={
            <View style={styles.emptyContainer}>
              <Text style={styles.emptyText}>No messages yet</Text>
              <Text style={styles.emptySubtext}>Start the conversation!</Text>
            </View>
          }
        />
      )}

      {typingUserIds.size > 0 && (
        <View style={styles.typingIndicator}>
          <Text style={styles.typingText}>Someone is typing...</Text>
        </View>
      )}

      <View style={styles.inputContainer}>
        <TouchableOpacity
          style={styles.videoCircleButton}
          onPress={() => setShowRecorder(true)}
        >
          <View style={styles.videoCircleIcon}>
            <View style={styles.videoCircleIconInner} />
          </View>
        </TouchableOpacity>
        <TextInput
          style={styles.input}
          placeholder="Type a message..."
          value={messageText}
          onChangeText={handleTextChange}
          multiline
          maxLength={4096}
          onKeyPress={(e) => {
            if (
              Platform.OS === 'web' &&
              e.nativeEvent.key === 'Enter' &&
              !(e.nativeEvent as any).shiftKey
            ) {
              e.preventDefault();
              handleSendMessage();
            }
          }}
        />
        <TouchableOpacity
          style={[
            styles.sendButton,
            !messageText.trim() && styles.sendButtonDisabled,
          ]}
          onPress={handleSendMessage}
          disabled={!messageText.trim()}
        >
          <Text style={styles.sendButtonText}>Send</Text>
        </TouchableOpacity>
      </View>

      <VideoCircleRecorder
        visible={showRecorder}
        onClose={() => setShowRecorder(false)}
        onSend={handleSendVideoCircle}
      />
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#fff',
  },
  loadingContainer: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
  },
  messagesList: {
    padding: 16,
  },
  emptyContainer: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    padding: 40,
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
  typingIndicator: {
    padding: 8,
    paddingHorizontal: 16,
    backgroundColor: '#f0f0f0',
  },
  typingText: {
    fontSize: 14,
    color: '#666',
    fontStyle: 'italic',
  },
  inputContainer: {
    flexDirection: 'row',
    padding: 16,
    borderTopWidth: 1,
    borderTopColor: '#f0f0f0',
    backgroundColor: '#fff',
  },
  input: {
    flex: 1,
    minHeight: 40,
    maxHeight: 100,
    borderWidth: 1,
    borderColor: '#ddd',
    borderRadius: 20,
    paddingHorizontal: 16,
    paddingVertical: 8,
    fontSize: 16,
    marginRight: 8,
  },
  sendButton: {
    backgroundColor: '#007AFF',
    paddingHorizontal: 20,
    paddingVertical: 10,
    borderRadius: 20,
    justifyContent: 'center',
  },
  sendButtonDisabled: {
    backgroundColor: '#ccc',
  },
  sendButtonText: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '600',
  },
  videoCircleButton: {
    width: 40,
    height: 40,
    borderRadius: 20,
    justifyContent: 'center',
    alignItems: 'center',
    marginRight: 8,
  },
  videoCircleIcon: {
    width: 26,
    height: 26,
    borderRadius: 13,
    borderWidth: 2,
    borderColor: '#007AFF',
    justifyContent: 'center',
    alignItems: 'center',
  },
  videoCircleIconInner: {
    width: 12,
    height: 12,
    borderRadius: 6,
    backgroundColor: '#007AFF',
  },
});
