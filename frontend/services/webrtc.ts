import {
  RTCPeerConnection as RTCPeerConnectionImpl,
  RTCSessionDescription as RTCSessionDescriptionImpl,
  RTCIceCandidate as RTCIceCandidateImpl,
  mediaDevices as mediaDevicesImpl,
} from './webrtcPlatform';

export type NetworkQuality = 'good' | 'fair' | 'poor' | 'unknown';

const FALLBACK_ICE_SERVERS: RTCIceServer[] = [
  { urls: 'stun:stun.relay.metered.ca:80' },
];

export interface WebRTCCallbacks {
  onRemoteStream: (stream: MediaStream) => void;
  onIceCandidate: (candidate: RTCIceCandidate) => void;
  onConnectionStateChange: (state: RTCPeerConnectionState) => void;
  onNetworkQuality: (quality: NetworkQuality) => void;
}

class WebRTCService {
  private peerConnection: RTCPeerConnection | null = null;
  private localStream: MediaStream | null = null;
  private callbacks: WebRTCCallbacks | null = null;
  private statsInterval: ReturnType<typeof setInterval> | null = null;
  private pendingCandidates: RTCIceCandidateInit[] = [];
  private hasRemoteDescription = false;

  async initialize(callbacks: WebRTCCallbacks, iceServers?: RTCIceServer[]): Promise<MediaStream> {
    this.callbacks = callbacks;

    this.localStream = await mediaDevicesImpl.getUserMedia({
      audio: true,
      video: {
        facingMode: 'user',
        width: { ideal: 640 },
        height: { ideal: 480 },
        frameRate: { ideal: 30 },
      },
    });

    this.peerConnection = new RTCPeerConnectionImpl({
      iceServers: iceServers ?? FALLBACK_ICE_SERVERS,
    });

    this.pendingCandidates = [];
    this.hasRemoteDescription = false;

    this.localStream.getTracks().forEach((track) => {
      this.peerConnection!.addTrack(track, this.localStream!);
    });

    this.peerConnection.onicecandidate = (event) => {
      if (event.candidate) {
        this.callbacks?.onIceCandidate(event.candidate);
      }
    };

    this.peerConnection.ontrack = (event) => {
      if (event.streams && event.streams[0]) {
        this.callbacks?.onRemoteStream(event.streams[0]);
      }
    };

    this.peerConnection.onconnectionstatechange = () => {
      if (this.peerConnection) {
        this.callbacks?.onConnectionStateChange(
          this.peerConnection.connectionState
        );
        if (this.peerConnection.connectionState === 'connected') {
          this.startQualityMonitoring();
        } else if (
          this.peerConnection.connectionState === 'disconnected' ||
          this.peerConnection.connectionState === 'failed' ||
          this.peerConnection.connectionState === 'closed'
        ) {
          this.stopQualityMonitoring();
        }
      }
    };

    return this.localStream;
  }

  async createOffer(): Promise<RTCSessionDescriptionInit> {
    if (!this.peerConnection) throw new Error('Not initialized');
    const offer = await this.peerConnection.createOffer({
      offerToReceiveAudio: true,
      offerToReceiveVideo: true,
    } as any);
    await this.peerConnection.setLocalDescription(offer);
    return offer;
  }

  async handleOffer(
    offer: RTCSessionDescriptionInit
  ): Promise<RTCSessionDescriptionInit> {
    if (!this.peerConnection) throw new Error('Not initialized');
    await this.peerConnection.setRemoteDescription(
      new RTCSessionDescriptionImpl(offer)
    );
    this.hasRemoteDescription = true;
    await this.flushPendingCandidates();
    const answer = await this.peerConnection.createAnswer();
    await this.peerConnection.setLocalDescription(answer);
    return answer;
  }

  async handleAnswer(answer: RTCSessionDescriptionInit): Promise<void> {
    if (!this.peerConnection) throw new Error('Not initialized');
    await this.peerConnection.setRemoteDescription(
      new RTCSessionDescriptionImpl(answer)
    );
    this.hasRemoteDescription = true;
    await this.flushPendingCandidates();
  }

  async addIceCandidate(candidate: RTCIceCandidateInit): Promise<void> {
    if (!this.peerConnection) return;
    if (!this.hasRemoteDescription) {
      this.pendingCandidates.push(candidate);
      return;
    }
    await this.peerConnection.addIceCandidate(new RTCIceCandidateImpl(candidate));
  }

  private async flushPendingCandidates(): Promise<void> {
    const candidates = this.pendingCandidates;
    this.pendingCandidates = [];
    for (const candidate of candidates) {
      try {
        await this.peerConnection?.addIceCandidate(new RTCIceCandidateImpl(candidate));
      } catch {
        // Best-effort
      }
    }
  }

  toggleAudio(enabled: boolean): void {
    this.localStream?.getAudioTracks().forEach((track) => {
      track.enabled = enabled;
    });
  }

  toggleVideo(enabled: boolean): void {
    this.localStream?.getVideoTracks().forEach((track) => {
      track.enabled = enabled;
    });
  }

  private startQualityMonitoring(): void {
    this.statsInterval = setInterval(async () => {
      if (!this.peerConnection) return;
      try {
        const stats = await this.peerConnection.getStats();
        let packetsLost = 0;
        let packetsReceived = 0;

        stats.forEach((report: any) => {
          if (report.type === 'inbound-rtp' && report.kind === 'video') {
            packetsLost += report.packetsLost || 0;
            packetsReceived += report.packetsReceived || 0;
          }
        });

        const totalPackets = packetsLost + packetsReceived;
        let quality: NetworkQuality = 'unknown';
        if (totalPackets > 0) {
          const lossRate = packetsLost / totalPackets;
          if (lossRate < 0.02) quality = 'good';
          else if (lossRate < 0.05) quality = 'fair';
          else quality = 'poor';
        }

        this.callbacks?.onNetworkQuality(quality);
      } catch {
        // Stats not available
      }
    }, 3000);
  }

  private stopQualityMonitoring(): void {
    if (this.statsInterval) {
      clearInterval(this.statsInterval);
      this.statsInterval = null;
    }
  }

  getLocalStream(): MediaStream | null {
    return this.localStream;
  }

  cleanup(): void {
    this.stopQualityMonitoring();
    this.localStream?.getTracks().forEach((track) => track.stop());
    this.localStream = null;
    this.peerConnection?.close();
    this.peerConnection = null;
    this.callbacks = null;
    this.pendingCandidates = [];
    this.hasRemoteDescription = false;
  }
}

export const webRTCService = new WebRTCService();
export default webRTCService;
