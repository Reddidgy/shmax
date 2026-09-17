import { create } from 'zustand';
import api from '../services/api';

interface User {
  id: string;
  username: string;
  display_name: string;
  avatar_url?: string;
}

interface Contact {
  id: string;
  user: User;
  created_at: string;
}

interface ContactState {
  contacts: Contact[];
  searchResults: User[];
  isLoading: boolean;
  error: string | null;

  // Actions
  fetchContacts: () => Promise<void>;
  addContact: (userId: string) => Promise<void>;
  removeContact: (contactId: string) => Promise<void>;
  searchUsers: (query: string) => Promise<void>;
  clearSearch: () => void;
}

export const useContactStore = create<ContactState>((set, get) => ({
  contacts: [],
  searchResults: [],
  isLoading: false,
  error: null,

  fetchContacts: async () => {
    try {
      set({ isLoading: true, error: null });
      const response = await api.contacts.getAll({ limit: 50 });
      set({ contacts: response.data, isLoading: false });
    } catch (error: any) {
      set({
        error: error.response?.data?.detail || 'Failed to fetch contacts',
        isLoading: false,
      });
    }
  },

  addContact: async (userId: string) => {
    try {
      set({ isLoading: true, error: null });
      const response = await api.contacts.add(userId);
      set((state) => ({
        contacts: [response.data, ...state.contacts],
        isLoading: false,
      }));
    } catch (error: any) {
      set({
        error: error.response?.data?.detail || 'Failed to add contact',
        isLoading: false,
      });
      throw error;
    }
  },

  removeContact: async (contactId: string) => {
    try {
      set({ isLoading: true, error: null });
      await api.contacts.remove(contactId);
      set((state) => ({
        contacts: state.contacts.filter((c) => c.id !== contactId),
        isLoading: false,
      }));
    } catch (error: any) {
      set({
        error: error.response?.data?.detail || 'Failed to remove contact',
        isLoading: false,
      });
      throw error;
    }
  },

  searchUsers: async (query: string) => {
    try {
      set({ isLoading: true, error: null });
      const response = await api.contacts.search(query);
      set({ searchResults: response.data, isLoading: false });
    } catch (error: any) {
      set({
        error: error.response?.data?.detail || 'Search failed',
        isLoading: false,
      });
    }
  },

  clearSearch: () => {
    set({ searchResults: [] });
  },
}));
