import { create } from 'zustand';
import api from '../services/api';

interface FriendRequest {
  id: string;
  from_user_id: string;
  to_user_id: string;
  status: string;
  created_at: string;
  updated_at: string;
  from_user?: { id: string; username: string; display_name: string; avatar_url?: string };
  to_user?: { id: string; username: string; display_name: string; avatar_url?: string };
}

interface InviteLink {
  id: string;
  code: string;
  is_active: boolean;
  invite_url?: string;
  created_at: string;
  expires_at: string;
}

interface FriendRequestStore {
  incomingRequests: FriendRequest[];
  outgoingRequests: FriendRequest[];
  inviteLink: InviteLink | null;
  isLoading: boolean;
  fetchIncoming: () => Promise<void>;
  fetchOutgoing: () => Promise<void>;
  sendRequest: (toUserId: string) => Promise<void>;
  acceptRequest: (requestId: string) => Promise<void>;
  declineRequest: (requestId: string) => Promise<void>;
  generateInviteLink: () => Promise<void>;
}

export const useFriendRequestStore = create<FriendRequestStore>((set, get) => ({
  incomingRequests: [],
  outgoingRequests: [],
  inviteLink: null,
  isLoading: false,

  fetchIncoming: async () => {
    try {
      const response = await api.friendRequests.getIncoming();
      set({ incomingRequests: response.data });
    } catch (error) {
      console.error('Failed to fetch incoming requests:', error);
    }
  },

  fetchOutgoing: async () => {
    try {
      const response = await api.friendRequests.getOutgoing();
      set({ outgoingRequests: response.data });
    } catch (error) {
      console.error('Failed to fetch outgoing requests:', error);
    }
  },

  sendRequest: async (toUserId: string) => {
    try {
      set({ isLoading: true });
      await api.friendRequests.send(toUserId);
      await get().fetchOutgoing();
    } catch (error) {
      console.error('Failed to send friend request:', error);
      throw error;
    } finally {
      set({ isLoading: false });
    }
  },

  acceptRequest: async (requestId: string) => {
    try {
      set({ isLoading: true });
      await api.friendRequests.accept(requestId);
      await get().fetchIncoming();
    } catch (error) {
      console.error('Failed to accept friend request:', error);
      throw error;
    } finally {
      set({ isLoading: false });
    }
  },

  declineRequest: async (requestId: string) => {
    try {
      set({ isLoading: true });
      await api.friendRequests.decline(requestId);
      await get().fetchIncoming();
    } catch (error) {
      console.error('Failed to decline friend request:', error);
      throw error;
    } finally {
      set({ isLoading: false });
    }
  },

  generateInviteLink: async () => {
    try {
      set({ isLoading: true });
      const response = await api.invites.create();
      set({ inviteLink: response.data });
    } catch (error) {
      console.error('Failed to generate invite link:', error);
      throw error;
    } finally {
      set({ isLoading: false });
    }
  },
}));
