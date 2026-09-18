import { useAuthStore } from '../store/authStore';
import { useChatStore } from '../store/chatStore';
import { useCallStore } from '../store/callStore';

import { WS_BASE_URL } from './config';

class WebSocketClient {
  private ws: WebSocket | null = null;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private reconnectDelay = 1000;
  private isConnecting = false;
  private reconnectCallbacks = new Set<() => void>();

  connect() {
    if (this.ws?.readyState === WebSocket.OPEN || this.isConnecting) {
      return;
    }

    const token = useAuthStore.getState().accessToken;
    if (!token) {
      console.log('No access token, cannot connect WebSocket');
      return;
    }

    this.isConnecting = true;

    try {
      this.ws = new WebSocket(`${WS_BASE_URL}/ws?token=${token}`);

      this.ws.onopen = () => {
        console.log('WebSocket connected');
        this.isConnecting = false;
        const wasReconnect = this.reconnectAttempts > 0;
        this.reconnectAttempts = 0;
        this.reconnectDelay = 1000;
        if (wasReconnect) {
          this.reconnectCallbacks.forEach((cb) => cb());
        }
      };

      this.ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          this.handleMessage(data);
        } catch (error) {
          console.error('WebSocket message parse error:', error);
        }
      };

      this.ws.onerror = (error) => {
        console.error('WebSocket error:', error);
        this.isConnecting = false;
      };

      this.ws.onclose = () => {
        console.log('WebSocket disconnected');
        this.isConnecting = false;
        this.ws = null;
        this.attemptReconnect();
      };
    } catch (error) {
      console.error('WebSocket connection error:', error);
      this.isConnecting = false;
      this.attemptReconnect();
    }
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.reconnectAttempts = this.maxReconnectAttempts;
  }

  private attemptReconnect() {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.log('Max reconnect attempts reached');
      return;
    }

    this.reconnectAttempts++;
    const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1);

    console.log(
      `Attempting to reconnect in ${delay}ms (attempt ${this.reconnectAttempts})`
    );

    setTimeout(() => {
      this.connect();
    }, delay);
  }

  private handleMessage(data: any) {
    const { type } = data;

    switch (type) {
      case 'new_message':
        useChatStore.getState().handleNewMessage(data);
        break;

      case 'message_state_update':
        useChatStore.getState().updateMessageState(
          data.message_id,
          data.state
        );
        break;

      case 'typing_indicator':
        useChatStore.getState().setTyping(
          data.conversation_id,
          data.user_id,
          data.is_typing
        );
        break;

      case 'presence_update':
        // Handle presence updates
        console.log('Presence update:', data);
        break;

      case 'friend_request_received':
        // Trigger a refresh of incoming friend requests
        console.log('Friend request received:', data);
        break;

      case 'friend_request_accepted':
        // Trigger a refresh - the sender's request was accepted
        console.log('Friend request accepted:', data);
        break;

      case 'incoming_call':
        useCallStore.getState().handleIncomingCall(data);
        break;

      case 'call_initiated':
        // Caller receives their call_id assignment — store it, but don't start WebRTC yet
        useCallStore.setState({ callId: data.call_id, iceServers: data.ice_servers ?? null });
        break;

      case 'call_accepted':
        useCallStore.getState().handleCallAccepted(data);
        break;

      case 'call_declined':
        useCallStore.getState().handleCallDeclined();
        break;

      case 'call_ended':
        useCallStore.getState().handleCallEnded();
        break;

      case 'call_error':
        useCallStore.getState().reset();
        break;

      case 'webrtc_offer':
        useCallStore.getState().handleWebRTCOffer(data);
        break;

      case 'webrtc_answer':
        useCallStore.getState().handleWebRTCAnswer(data);
        break;

      case 'ice_candidate':
        useCallStore.getState().handleIceCandidate(data);
        break;

      default:
        console.log('Unknown WebSocket message type:', type);
    }
  }

  send(data: any) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    } else {
      console.warn('WebSocket not connected, cannot send message');
    }
  }

  sendTypingStart(conversationId: string) {
    this.send({
      type: 'typing_start',
      conversation_id: conversationId,
    });
  }

  sendTypingStop(conversationId: string) {
    this.send({
      type: 'typing_stop',
      conversation_id: conversationId,
    });
  }

  sendMessageRead(messageId: string, conversationId: string) {
    this.send({
      type: 'message_read',
      message_id: messageId,
      conversation_id: conversationId,
    });
  }

  onReconnect(callback: () => void): () => void {
    this.reconnectCallbacks.add(callback);
    return () => this.reconnectCallbacks.delete(callback);
  }

  isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}

export const websocketClient = new WebSocketClient();
export default websocketClient;
