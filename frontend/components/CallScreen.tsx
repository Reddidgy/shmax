import {
  View,
  Text,
  TouchableOpacity,
  StyleSheet,
  Platform,
} from 'react-native';
import { useCallStore, CallState } from '../store/callStore';
import RTCVideoView from './RTCVideoView';

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

function getQualityColor(quality: string): string {
  switch (quality) {
    case 'good':
      return '#4CD964';
    case 'fair':
      return '#FF9500';
    case 'poor':
      return '#FF3B30';
    default:
      return '#8E8E93';
  }
}

function getStatusText(callState: CallState): string {
  switch (callState) {
    case 'outgoing':
      return 'Calling...';
    case 'connecting':
      return 'Connecting...';
    case 'connected':
      return '';
    case 'ended':
      return 'Call Ended';
    default:
      return '';
  }
}

export default function CallScreen() {
  const {
    callState,
    callee,
    caller,
    localStream,
    remoteStream,
    isAudioEnabled,
    isVideoEnabled,
    networkQuality,
    callDuration,
    endCall,
    toggleAudio,
    toggleVideo,
  } = useCallStore();

  const peer = callee || caller;
  const showControls =
    callState === 'outgoing' ||
    callState === 'connecting' ||
    callState === 'connected';
  const statusText = getStatusText(callState);

  if (callState === 'idle' || callState === 'incoming') {
    return null;
  }

  const renderRemoteVideo = () => {
    if (remoteStream) {
      return (
        <RTCVideoView
          stream={remoteStream}
          style={Platform.OS === 'web'
            ? { width: '100%', height: '100%', backgroundColor: '#000' }
            : styles.remoteVideo}
        />
      );
    }

    return (
      <View style={styles.noVideoContainer}>
        <View style={styles.peerAvatarLarge}>
          <Text style={styles.peerAvatarLargeText}>
            {peer?.display_name?.[0]?.toUpperCase() || '?'}
          </Text>
        </View>
        <Text style={styles.peerNameLarge}>{peer?.display_name}</Text>
      </View>
    );
  };

  const renderLocalVideo = () => {
    if (!localStream || !isVideoEnabled) return null;

    return (
      <View style={styles.localVideoContainer}>
        <RTCVideoView
          stream={localStream}
          muted
          mirror
          style={Platform.OS === 'web'
            ? { width: '100%', height: '100%', borderRadius: 12 }
            : styles.localVideo}
        />
      </View>
    );
  };

  return (
    <View style={styles.container}>
      <View style={styles.remoteVideoContainer}>{renderRemoteVideo()}</View>

      {renderLocalVideo()}

      <View style={styles.topBar}>
        {statusText ? (
          <Text style={styles.statusText}>{statusText}</Text>
        ) : null}
        {callState === 'connected' && (
          <Text style={styles.durationText}>
            {formatDuration(callDuration)}
          </Text>
        )}
        {callState === 'connected' && networkQuality !== 'unknown' && (
          <View style={styles.qualityBadge}>
            <View
              style={[
                styles.qualityDot,
                { backgroundColor: getQualityColor(networkQuality) },
              ]}
            />
            <Text style={styles.qualityText}>{networkQuality}</Text>
          </View>
        )}
      </View>

      {showControls && (
        <View style={styles.controlsContainer}>
          <TouchableOpacity
            style={[
              styles.controlButton,
              !isAudioEnabled && styles.controlButtonActive,
            ]}
            onPress={toggleAudio}
          >
            <Text style={styles.controlIcon}>
              {isAudioEnabled ? '🎤' : '🔇'}
            </Text>
            <Text style={styles.controlLabel}>
              {isAudioEnabled ? 'Mute' : 'Unmute'}
            </Text>
          </TouchableOpacity>

          <TouchableOpacity
            style={[
              styles.controlButton,
              !isVideoEnabled && styles.controlButtonActive,
            ]}
            onPress={toggleVideo}
          >
            <Text style={styles.controlIcon}>
              {isVideoEnabled ? '📹' : '📷'}
            </Text>
            <Text style={styles.controlLabel}>
              {isVideoEnabled ? 'Camera Off' : 'Camera On'}
            </Text>
          </TouchableOpacity>

          <TouchableOpacity
            style={[styles.controlButton, styles.endCallButton]}
            onPress={endCall}
          >
            <Text style={styles.controlIcon}>📞</Text>
            <Text style={[styles.controlLabel, { color: '#fff' }]}>
              End
            </Text>
          </TouchableOpacity>
        </View>
      )}

      {callState === 'ended' && (
        <View style={styles.endedOverlay}>
          <Text style={styles.endedText}>Call Ended</Text>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: '#000',
    zIndex: 1000,
  },
  remoteVideoContainer: {
    flex: 1,
    backgroundColor: '#1a1a1a',
  },
  remoteVideo: {
    flex: 1,
  },
  noVideoContainer: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    backgroundColor: '#1a1a1a',
  },
  peerAvatarLarge: {
    width: 120,
    height: 120,
    borderRadius: 60,
    backgroundColor: '#007AFF',
    justifyContent: 'center',
    alignItems: 'center',
    marginBottom: 16,
  },
  peerAvatarLargeText: {
    color: '#fff',
    fontSize: 48,
    fontWeight: 'bold',
  },
  peerNameLarge: {
    color: '#fff',
    fontSize: 24,
    fontWeight: '600',
  },
  localVideoContainer: {
    position: 'absolute',
    top: 60,
    right: 16,
    width: 120,
    height: 160,
    borderRadius: 12,
    overflow: 'hidden',
    borderWidth: 2,
    borderColor: '#fff',
    elevation: 5,
    ...(Platform.OS === 'web'
      ? { boxShadow: '0 2px 8px rgba(0,0,0,0.3)' }
      : { shadowColor: '#000', shadowOffset: { width: 0, height: 2 }, shadowOpacity: 0.3, shadowRadius: 4 }),
  },
  localVideo: {
    width: '100%',
    height: '100%',
  },
  topBar: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    paddingTop: Platform.OS === 'ios' ? 60 : 40,
    paddingHorizontal: 16,
    paddingBottom: 16,
    alignItems: 'center',
  },
  statusText: {
    color: '#fff',
    fontSize: 18,
    fontWeight: '500',
    marginBottom: 4,
  },
  durationText: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '600',
    marginBottom: 4,
  },
  qualityBadge: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: 'rgba(0,0,0,0.5)',
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 12,
  },
  qualityDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    marginRight: 6,
  },
  qualityText: {
    color: '#fff',
    fontSize: 12,
    textTransform: 'capitalize',
  },
  controlsContainer: {
    position: 'absolute',
    bottom: 0,
    left: 0,
    right: 0,
    flexDirection: 'row',
    justifyContent: 'center',
    alignItems: 'center',
    paddingBottom: Platform.OS === 'ios' ? 40 : 24,
    paddingTop: 16,
    backgroundColor: 'rgba(0,0,0,0.4)',
    gap: 24,
  },
  controlButton: {
    width: 64,
    height: 64,
    borderRadius: 32,
    backgroundColor: 'rgba(255,255,255,0.2)',
    justifyContent: 'center',
    alignItems: 'center',
  },
  controlButtonActive: {
    backgroundColor: 'rgba(255,255,255,0.4)',
  },
  endCallButton: {
    backgroundColor: '#FF3B30',
  },
  controlIcon: {
    fontSize: 24,
  },
  controlLabel: {
    color: '#fff',
    fontSize: 10,
    marginTop: 2,
  },
  endedOverlay: {
    ...StyleSheet.absoluteFillObject,
    justifyContent: 'center',
    alignItems: 'center',
    backgroundColor: 'rgba(0,0,0,0.7)',
  },
  endedText: {
    color: '#fff',
    fontSize: 24,
    fontWeight: '600',
  },
});
