import { create } from 'zustand';
import * as SecureStore from 'expo-secure-store';
import { Platform } from 'react-native';
import api from '../services/api';
import websocketClient from '../services/websocket';

interface User {
  id: string;
  username: string;
  display_name: string;
  avatar_url?: string;
  is_active: boolean;
  last_seen?: string;
  created_at: string;
}

interface AuthState {
  user: User | null;
  accessToken: string | null;
  refreshToken: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;

  // Actions
  login: (username: string, password: string) => Promise<void>;
  register: (data: {
    username: string;
    password: string;
    display_name: string;
    email?: string;
    phone?: string;
  }) => Promise<void>;
  logout: () => Promise<void>;
  refreshTokens: () => Promise<void>;
  loadStoredTokens: () => Promise<void>;
  fetchUser: () => Promise<void>;
}

// Token storage helpers
const storeToken = async (key: string, value: string) => {
  if (Platform.OS === 'web') {
    localStorage.setItem(key, value);
  } else {
    await SecureStore.setItemAsync(key, value);
  }
};

const getToken = async (key: string): Promise<string | null> => {
  if (Platform.OS === 'web') {
    return localStorage.getItem(key);
  } else {
    return await SecureStore.getItemAsync(key);
  }
};

const removeToken = async (key: string) => {
  if (Platform.OS === 'web') {
    localStorage.removeItem(key);
  } else {
    await SecureStore.deleteItemAsync(key);
  }
};

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  accessToken: null,
  refreshToken: null,
  isAuthenticated: false,
  isLoading: false,
  error: null,

  login: async (username, password) => {
    try {
      set({ isLoading: true, error: null });
      const response = await api.auth.login({ username, password });
      const { access_token, refresh_token } = response.data;

      await storeToken('accessToken', access_token);
      await storeToken('refreshToken', refresh_token);

      set({
        accessToken: access_token,
        refreshToken: refresh_token,
        isAuthenticated: true,
        isLoading: false,
      });

      await get().fetchUser();
      websocketClient.connect();
    } catch (error: any) {
      set({
        error: error.response?.data?.detail || 'Login failed',
        isLoading: false,
      });
      throw error;
    }
  },

  register: async (data) => {
    try {
      set({ isLoading: true, error: null });
      const response = await api.auth.register(data);
      const { access_token, refresh_token } = response.data;

      await storeToken('accessToken', access_token);
      await storeToken('refreshToken', refresh_token);

      set({
        accessToken: access_token,
        refreshToken: refresh_token,
        isAuthenticated: true,
        isLoading: false,
      });

      await get().fetchUser();
      websocketClient.connect();
    } catch (error: any) {
      set({
        error: error.response?.data?.detail || 'Registration failed',
        isLoading: false,
      });
      throw error;
    }
  },

  logout: async () => {
    const refreshToken = get().refreshToken;
    try {
      if (refreshToken) {
        await api.auth.logout(refreshToken);
      }
    } catch (error) {
      console.error('Logout error:', error);
    }

    websocketClient.disconnect();
    await removeToken('accessToken');
    await removeToken('refreshToken');

    set({
      user: null,
      accessToken: null,
      refreshToken: null,
      isAuthenticated: false,
      error: null,
    });
  },

  refreshTokens: async () => {
    const refreshToken = get().refreshToken;
    if (!refreshToken) {
      throw new Error('No refresh token available');
    }

    try {
      const response = await api.auth.refresh(refreshToken);
      const { access_token, refresh_token } = response.data;

      await storeToken('accessToken', access_token);
      await storeToken('refreshToken', refresh_token);

      set({
        accessToken: access_token,
        refreshToken: refresh_token,
      });
    } catch (error) {
      await get().logout();
      throw error;
    }
  },

  loadStoredTokens: async () => {
    try {
      set({ isLoading: true });
      const accessToken = await getToken('accessToken');
      const refreshToken = await getToken('refreshToken');

      if (accessToken && refreshToken) {
        set({
          accessToken,
          refreshToken,
          isAuthenticated: true,
        });

        try {
          await get().fetchUser();
          websocketClient.connect();
        } catch (error) {
          // If fetching user fails, try refreshing tokens
          try {
            await get().refreshTokens();
            await get().fetchUser();
            websocketClient.connect();
          } catch (refreshError) {
            await get().logout();
          }
        }
      }
    } catch (error) {
      console.error('Error loading stored tokens:', error);
    } finally {
      set({ isLoading: false });
    }
  },

  fetchUser: async () => {
    try {
      const response = await api.auth.getMe();
      set({ user: response.data });
    } catch (error) {
      console.error('Error fetching user:', error);
      throw error;
    }
  },
}));
