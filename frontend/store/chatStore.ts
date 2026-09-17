import { create } from 'zustand';
import api from '../services/api';

interface User {
  id: string;
  username: string;
  display_name: string;
  avatar_url?: string;
}

interface Message {
  id: string;
  conversation_id: string;
  sender: User;
  content?: string;
  message_type: 'text' | 'video_circle' | 'image';
  media_url?: string;
  thumbnail_url?: string;
  state: 'sent' | 'delivered' | 'read';
  created_at: string;
}

interface Conversation {
  id: string;
  title?: string;
  is_group: boolean;
  participants: User[];
  last_message?: Message;
  unread_count: number;
  created_at: string;
  updated_at: string;
}

interface ChatState {
  conversations: Conversation[];
  messages: Record<string, Message[]>;
  typingUsers: Record<string, Set<string>>;
  isLoading: boolean;
  error: string | null;

  // Actions
  fetchConversations: () => Promise<void>;
  fetchMessages: (conversationId: string) => Promise<void>;
  sendMessage: (
    conversationId: string,
    content: string | null,
    messageType?: string,
    mediaUrl?: string,
    thumbnailUrl?: string
  ) => Promise<void>;
  createConversation: (participantIds: string[], title?: string) => Promise<string>;
  updateMessageState: (messageId: string, state: string) => void;
  setTyping: (conversationId: string, userId: string, isTyping: boolean) => void;
  handleNewMessage: (data: any) => void;
}

export const useChatStore = create<ChatState>((set, get) => ({
  conversations: [],
  messages: {},
  typingUsers: {},
  isLoading: false,
  error: null,

  fetchConversations: async () => {
    try {
      set({ isLoading: true, error: null });
      const response = await api.conversations.getAll({ limit: 50 });
      set({ conversations: response.data, isLoading: false });
    } catch (error: any) {
      set({
        error: error.response?.data?.detail || 'Failed to fetch conversations',
        isLoading: false,
      });
    }
  },

  fetchMessages: async (conversationId: string) => {
    try {
      set({ isLoading: true, error: null });
      const response = await api.conversations.getMessages(conversationId, {
        limit: 30,
      });
      set((state) => {
        const fetched: Message[] = response.data;
        const fetchedIds = new Set(fetched.map((m) => m.id));
        const existing = state.messages[conversationId] || [];
        const realtimeOnly = existing.filter((m) => !fetchedIds.has(m.id));
        return {
          messages: {
            ...state.messages,
            [conversationId]: [...fetched, ...realtimeOnly],
          },
          isLoading: false,
        };
      });
    } catch (error: any) {
      set({
        error: error.response?.data?.detail || 'Failed to fetch messages',
        isLoading: false,
      });
    }
  },

  sendMessage: async (
    conversationId,
    content,
    messageType = 'text',
    mediaUrl,
    thumbnailUrl
  ) => {
    try {
      const response = await api.conversations.sendMessage(conversationId, {
        content,
        message_type: messageType,
        media_url: mediaUrl,
        thumbnail_url: thumbnailUrl,
      });

      // Add message to local state
      set((state) => ({
        messages: {
          ...state.messages,
          [conversationId]: [
            ...(state.messages[conversationId] || []),
            response.data,
          ],
        },
      }));

      // Update conversation list
      await get().fetchConversations();
    } catch (error: any) {
      set({
        error: error.response?.data?.detail || 'Failed to send message',
      });
      throw error;
    }
  },

  createConversation: async (participantIds, title) => {
    try {
      set({ isLoading: true, error: null });
      const response = await api.conversations.create({
        participant_ids: participantIds,
        title,
      });
      set((state) => ({
        conversations: [response.data, ...state.conversations],
        isLoading: false,
      }));
      return response.data.id;
    } catch (error: any) {
      set({
        error: error.response?.data?.detail || 'Failed to create conversation',
        isLoading: false,
      });
      throw error;
    }
  },

  updateMessageState: (messageId, newState) => {
    set((state) => {
      const updatedMessages = { ...state.messages };
      Object.keys(updatedMessages).forEach((conversationId) => {
        updatedMessages[conversationId] = updatedMessages[conversationId].map(
          (msg) =>
            msg.id === messageId
              ? { ...msg, state: newState as any }
              : msg
        );
      });
      return { messages: updatedMessages };
    });
  },

  setTyping: (conversationId, userId, isTyping) => {
    set((state) => {
      const typingUsers = { ...state.typingUsers };
      if (!typingUsers[conversationId]) {
        typingUsers[conversationId] = new Set();
      }

      if (isTyping) {
        typingUsers[conversationId].add(userId);
      } else {
        typingUsers[conversationId].delete(userId);
      }

      return { typingUsers };
    });
  },

  handleNewMessage: (data) => {
    const { message, conversation_id } = data;

    set((state) => {
      const existing = state.messages[conversation_id] || [];
      if (existing.some((m) => m.id === message.id)) {
        return state;
      }
      return {
        messages: {
          ...state.messages,
          [conversation_id]: [...existing, message],
        },
      };
    });

    get().fetchConversations();
  },
}));
