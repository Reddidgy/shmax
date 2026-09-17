import { create } from 'zustand';
import webRTCService, { NetworkQuality } from '../services/webrtc';

function getWsClient() {
  return require('../services/websocket').default;
}

export type CallState =
  | 'idle'
  | 'outgoing'
  | 'incoming'
  | 'connecting'
  | 'connected'
  | 'ended';

interface CallPeer {
  id: string;
  display_name: string;
  avatar_url?: string;
}

interface CallStoreState {
  callState: CallState;
  callId: string | null;
  caller: CallPeer | null;
  callee: CallPeer | null;
  localStream: MediaStream | null;
  remoteStream: MediaStream | null;
  isAudioEnabled: boolean;
  isVideoEnabled: boolean;
  networkQuality: NetworkQuality;
  callDuration: number;
  error: string | null;

  initiateCall: (peer: CallPeer) => Promise<void>;
  handleIncomingCall: (data: {
    call_id: string;
    caller: CallPeer;
  }) => void;
  acceptCall: () => Promise<void>;
  declineCall: () => void;
  endCall: () => void;
  handleCallAccepted: (data: { call_id: string }) => void;
  handleCallDeclined: () => void;
  handleCallEnded: () => void;
  handleWebRTCOffer: (data: {
    call_id: string;
    sdp: RTCSessionDescriptionInit;
  }) => Promise<void>;
  handleWebRTCAnswer: (data: {
    call_id: string;
    sdp: RTCSessionDescriptionInit;
  }) => Promise<void>;
  handleIceCandidate: (data: {
    call_id: string;
    candidate: RTCIceCandidateInit;
  }) => Promise<void>;
  toggleAudio: () => void;
  toggleVideo: () => void;
  reset: () => void;
}

let ringTimeout: ReturnType<typeof setTimeout> | null = null;
let durationInterval: ReturnType<typeof setInterval> | null = null;
let disconnectTimeout: ReturnType<typeof setTimeout> | null = null;

function clearTimers() {
  if (ringTimeout) {
    clearTimeout(ringTimeout);
    ringTimeout = null;
  }
  if (durationInterval) {
    clearInterval(durationInterval);
    durationInterval = null;
  }
  if (disconnectTimeout) {
    clearTimeout(disconnectTimeout);
    disconnectTimeout = null;
  }
}

export const useCallStore = create<CallStoreState>((set, get) => ({
  callState: 'idle',
  callId: null,
  caller: null,
  callee: null,
  localStream: null,
  remoteStream: null,
  isAudioEnabled: true,
  isVideoEnabled: true,
  networkQuality: 'unknown',
  callDuration: 0,
  error: null,

  initiateCall: async (peer: CallPeer) => {
    try {
      const localStream = await webRTCService.initialize({
        onRemoteStream: (stream) => set({ remoteStream: stream }),
        onIceCandidate: (candidate) => {
          const { callId } = get();
          if (callId) {
            getWsClient().send({
              type: 'ice_candidate',
              call_id: callId,
              candidate: candidate.toJSON(),
            });
          }
        },
        onConnectionStateChange: (state) => {
          if (state === 'connected') {
            if (disconnectTimeout) {
              clearTimeout(disconnectTimeout);
              disconnectTimeout = null;
            }
            set({ callState: 'connected' });
            durationInterval = setInterval(() => {
              set((s) => ({ callDuration: s.callDuration + 1 }));
            }, 1000);
          } else if (state === 'failed') {
            get().endCall();
          } else if (state === 'disconnected') {
            if (!disconnectTimeout) {
              disconnectTimeout = setTimeout(() => {
                disconnectTimeout = null;
                get().endCall();
              }, 15000);
            }
          }
        },
        onNetworkQuality: (quality) => set({ networkQuality: quality }),
      });

      set({
        callState: 'outgoing',
        callee: peer,
        localStream,
        isAudioEnabled: true,
        isVideoEnabled: true,
        callDuration: 0,
        error: null,
      });

      getWsClient().send({
        type: 'call_initiate',
        callee_id: peer.id,
      });

      ringTimeout = setTimeout(() => {
        if (get().callState === 'outgoing') {
          get().endCall();
        }
      }, 30000);
    } catch (error: any) {
      set({ error: 'Failed to access camera/microphone', callState: 'idle' });
      webRTCService.cleanup();
    }
  },

  handleIncomingCall: (data) => {
    if (get().callState !== 'idle') {
      getWsClient().send({
        type: 'call_decline',
        call_id: data.call_id,
      });
      return;
    }

    set({
      callState: 'incoming',
      callId: data.call_id,
      caller: data.caller,
      error: null,
    });

    ringTimeout = setTimeout(() => {
      if (get().callState === 'incoming') {
        get().declineCall();
      }
    }, 30000);
  },

  acceptCall: async () => {
    const { callId } = get();
    if (!callId) return;

    clearTimers();

    try {
      const localStream = await webRTCService.initialize({
        onRemoteStream: (stream) => set({ remoteStream: stream }),
        onIceCandidate: (candidate) => {
          getWsClient().send({
            type: 'ice_candidate',
            call_id: callId,
            candidate: candidate.toJSON(),
          });
        },
        onConnectionStateChange: (state) => {
          if (state === 'connected') {
            if (disconnectTimeout) {
              clearTimeout(disconnectTimeout);
              disconnectTimeout = null;
            }
            set({ callState: 'connected' });
            durationInterval = setInterval(() => {
              set((s) => ({ callDuration: s.callDuration + 1 }));
            }, 1000);
          } else if (state === 'failed') {
            get().endCall();
          } else if (state === 'disconnected') {
            if (!disconnectTimeout) {
              disconnectTimeout = setTimeout(() => {
                disconnectTimeout = null;
                get().endCall();
              }, 15000);
            }
          }
        },
        onNetworkQuality: (quality) => set({ networkQuality: quality }),
      });

      set({
        callState: 'connecting',
        localStream,
        isAudioEnabled: true,
        isVideoEnabled: true,
        callDuration: 0,
      });

      getWsClient().send({
        type: 'call_accept',
        call_id: callId,
      });
    } catch (error: any) {
      set({ error: 'Failed to access camera/microphone' });
      get().declineCall();
    }
  },

  declineCall: () => {
    const { callId } = get();
    clearTimers();

    if (callId) {
      getWsClient().send({
        type: 'call_decline',
        call_id: callId,
      });
    }

    webRTCService.cleanup();
    set({
      callState: 'idle',
      callId: null,
      caller: null,
      callee: null,
      localStream: null,
      remoteStream: null,
      error: null,
    });
  },

  endCall: () => {
    const { callId } = get();
    clearTimers();

    if (callId) {
      getWsClient().send({
        type: 'call_end',
        call_id: callId,
      });
    }

    webRTCService.cleanup();
    set({
      callState: 'ended',
      localStream: null,
      remoteStream: null,
    });

    setTimeout(() => {
      set({
        callState: 'idle',
        callId: null,
        caller: null,
        callee: null,
        callDuration: 0,
        networkQuality: 'unknown',
        error: null,
      });
    }, 2000);
  },

  handleCallAccepted: async (data) => {
    clearTimers();
    set({ callState: 'connecting', callId: data.call_id });

    try {
      const offer = await webRTCService.createOffer();
      getWsClient().send({
        type: 'webrtc_offer',
        call_id: data.call_id,
        sdp: offer,
      });
    } catch (error) {
      get().endCall();
    }
  },

  handleCallDeclined: () => {
    clearTimers();
    webRTCService.cleanup();
    set({
      callState: 'ended',
      localStream: null,
      remoteStream: null,
    });
    setTimeout(() => {
      set({
        callState: 'idle',
        callId: null,
        caller: null,
        callee: null,
        error: null,
      });
    }, 2000);
  },

  handleCallEnded: () => {
    clearTimers();
    webRTCService.cleanup();
    set({
      callState: 'ended',
      localStream: null,
      remoteStream: null,
    });
    setTimeout(() => {
      set({
        callState: 'idle',
        callId: null,
        caller: null,
        callee: null,
        callDuration: 0,
        networkQuality: 'unknown',
        error: null,
      });
    }, 2000);
  },

  handleWebRTCOffer: async (data) => {
    try {
      const answer = await webRTCService.handleOffer(data.sdp);
      getWsClient().send({
        type: 'webrtc_answer',
        call_id: data.call_id,
        sdp: answer,
      });
    } catch (error) {
      get().endCall();
    }
  },

  handleWebRTCAnswer: async (data) => {
    try {
      await webRTCService.handleAnswer(data.sdp);
    } catch (error) {
      get().endCall();
    }
  },

  handleIceCandidate: async (data) => {
    try {
      await webRTCService.addIceCandidate(data.candidate);
    } catch {
      // Non-critical
    }
  },

  toggleAudio: () => {
    const { isAudioEnabled } = get();
    const newState = !isAudioEnabled;
    webRTCService.toggleAudio(newState);
    set({ isAudioEnabled: newState });
  },

  toggleVideo: () => {
    const { isVideoEnabled } = get();
    const newState = !isVideoEnabled;
    webRTCService.toggleVideo(newState);
    set({ isVideoEnabled: newState });
  },

  reset: () => {
    clearTimers();
    webRTCService.cleanup();
    set({
      callState: 'idle',
      callId: null,
      caller: null,
      callee: null,
      localStream: null,
      remoteStream: null,
      isAudioEnabled: true,
      isVideoEnabled: true,
      networkQuality: 'unknown',
      callDuration: 0,
      error: null,
    });
  },
}));
