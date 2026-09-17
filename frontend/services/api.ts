import axios, { AxiosInstance, InternalAxiosRequestConfig } from 'axios';
import { useAuthStore } from '../store/authStore';

import { API_BASE_URL } from './config';

class ApiClient {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: API_BASE_URL,
      timeout: 10000,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Request interceptor - add auth token
    this.client.interceptors.request.use(
      async (config: InternalAxiosRequestConfig) => {
        const token = useAuthStore.getState().accessToken;
        if (token) {
          config.headers.Authorization = `Bearer ${token}`;
        }
        return config;
      },
      (error) => Promise.reject(error)
    );

    // Response interceptor - handle token refresh
    this.client.interceptors.response.use(
      (response) => response,
      async (error) => {
        const originalRequest = error.config;

        if (error.response?.status === 401 && !originalRequest._retry) {
          originalRequest._retry = true;

          try {
            await useAuthStore.getState().refreshTokens();
            const token = useAuthStore.getState().accessToken;
            if (token) {
              originalRequest.headers.Authorization = `Bearer ${token}`;
            }
            return this.client(originalRequest);
          } catch (refreshError) {
            await useAuthStore.getState().logout();
            return Promise.reject(refreshError);
          }
        }

        return Promise.reject(error);
      }
    );
  }

  get instance() {
    return this.client;
  }

  // Auth endpoints
  auth = {
    register: (data: any) => this.client.post('/auth/register', data),
    login: (data: any) => this.client.post('/auth/login', data),
    refresh: (refreshToken: string) =>
      this.client.post('/auth/refresh', { refresh_token: refreshToken }),
    logout: (refreshToken: string) =>
      this.client.post('/auth/logout', { refresh_token: refreshToken }),
    getMe: () => this.client.get('/auth/me'),
  };

  // Contacts endpoints
  contacts = {
    getAll: (params?: { limit?: number; cursor?: string }) =>
      this.client.get('/contacts', { params }),
    add: (contactUserId: string) =>
      this.client.post('/contacts', { contact_user_id: contactUserId }),
    remove: (contactId: string) => this.client.delete(`/contacts/${contactId}`),
    search: (query: string) =>
      this.client.get('/contacts/search', { params: { q: query } }),
  };

  // Chat endpoints
  conversations = {
    getAll: (params?: { limit?: number; cursor?: string }) =>
      this.client.get('/conversations', { params }),
    getById: (id: string) => this.client.get(`/conversations/${id}`),
    create: (data: { participant_ids: string[]; title?: string }) =>
      this.client.post('/conversations', data),
    getMessages: (
      conversationId: string,
      params?: { limit?: number; cursor?: string }
    ) => this.client.get(`/conversations/${conversationId}/messages`, { params }),
    sendMessage: (conversationId: string, data: any) =>
      this.client.post(`/conversations/${conversationId}/messages`, data),
  };

  // Friend requests endpoints
  friendRequests = {
    getIncoming: () => this.client.get('/friend-requests/incoming'),
    getOutgoing: () => this.client.get('/friend-requests/outgoing'),
    send: (toUserId: string) =>
      this.client.post('/friend-requests', { to_user_id: toUserId }),
    accept: (requestId: string) =>
      this.client.post(`/friend-requests/${requestId}/accept`),
    decline: (requestId: string) =>
      this.client.post(`/friend-requests/${requestId}/decline`),
  };

  // Invites endpoints
  invites = {
    create: (expiresInDays?: number) =>
      this.client.post('/invites', { expires_in_days: expiresInDays || 7 }),
    resolve: (code: string) => this.client.get(`/invites/${code}`),
    accept: (code: string) => this.client.post(`/invites/${code}/accept`),
    deactivate: (inviteId: string) => this.client.delete(`/invites/${inviteId}`),
  };
}

export const api = new ApiClient();
export default api;
