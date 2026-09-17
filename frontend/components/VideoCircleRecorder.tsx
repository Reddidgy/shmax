import { useEffect, useRef, useState } from 'react';
import {
  View,
  Text,
  TouchableOpacity,
  StyleSheet,
  Platform,
} from 'react-native';
import {
  CameraView,
  useCameraPermissions,
  useMicrophonePermissions,
} from 'expo-camera/next';
import { Video, ResizeMode } from 'expo-av';

const MAX_DURATION = 60; // seconds
const VIEWFINDER_SIZE = 240;

interface VideoCircleRecorderProps {
  visible: boolean;
  onClose: () => void;
  onSend: (videoUri: string) => void;
}

type Mode = 'camera' | 'recording' | 'preview';

function formatTime(totalSeconds: number): string {
  const mins = Math.floor(totalSeconds / 60);
  const secs = totalSeconds % 60;
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

export default function VideoCircleRecorder({
  visible,
  onClose,
  onSend,
}: VideoCircleRecorderProps) {
  const [cameraPermission, requestCameraPermission] = useCameraPermissions();
  const [micPermission, requestMicPermission] = useMicrophonePermissions();
  const [facing, setFacing] = useState<'front' | 'back'>('front');
  const [mode, setMode] = useState<Mode>('camera');
  const [elapsed, setElapsed] = useState(0);
  const [recordedUri, setRecordedUri] = useState<string | null>(null);

  const cameraRef = useRef<CameraView>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const webRecorderRef = useRef<any>(null);
  const webChunksRef = useRef<Blob[]>([]);

  // Request permissions when the recorder opens
  useEffect(() => {
    if (!visible) return;
    if (!cameraPermission?.granted) {
      requestCameraPermission();
    }
    if (!micPermission?.granted) {
      requestMicPermission();
    }
  }, [visible]);

  // Reset state whenever the recorder is (re)opened
  useEffect(() => {
    if (visible) {
      setMode('camera');
      setRecordedUri(null);
      setElapsed(0);
    } else {
      stopTimer();
    }
    return () => {
      stopTimer();
    };
  }, [visible]);

  const startTimer = () => {
    stopTimer();
    timerRef.current = setInterval(() => {
      setElapsed((prev) => {
        const next = prev + 1;
        if (next >= MAX_DURATION) {
          stopRecording();
        }
        return next;
      });
    }, 1000);
  };

  const stopTimer = () => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  };

  const handleRecordingFinished = (uri: string) => {
    stopTimer();
    setRecordedUri(uri);
    setMode('preview');
  };

  const startWebRecording = async () => {
    try {
      // react-native-web renders CameraView as an underlying <video> element.
      // Try to reuse its live stream; fall back to requesting our own.
      let stream: MediaStream | null = null;
      if (typeof document !== 'undefined') {
        const videoEl = document.querySelector('video') as HTMLVideoElement | null;
        if (videoEl && videoEl.srcObject instanceof MediaStream) {
          stream = videoEl.srcObject;
        }
      }
      if (!stream && typeof navigator !== 'undefined' && navigator.mediaDevices) {
        stream = await navigator.mediaDevices.getUserMedia({
          video: true,
          audio: true,
        });
      }
      if (!stream) {
        throw new Error('No media stream available for web recording');
      }

      const recorder = new (window as any).MediaRecorder(stream, {
        mimeType: 'video/webm',
      });
      webChunksRef.current = [];
      recorder.ondataavailable = (e: any) => {
        if (e.data && e.data.size > 0) {
          webChunksRef.current.push(e.data);
        }
      };
      recorder.onstop = () => {
        const blob = new Blob(webChunksRef.current, { type: 'video/webm' });
        const url = URL.createObjectURL(blob);
        handleRecordingFinished(url);
      };
      webRecorderRef.current = recorder;
      recorder.start();
    } catch (e) {
      console.error('Web recording failed to start:', e);
      stopTimer();
      setMode('camera');
    }
  };

  const startRecording = async () => {
    setMode('recording');
    setElapsed(0);
    startTimer();

    if (Platform.OS === 'web') {
      await startWebRecording();
      return;
    }

    try {
      const result = await cameraRef.current?.recordAsync({
        maxDuration: MAX_DURATION,
      });
      if (result?.uri) {
        handleRecordingFinished(result.uri);
      } else {
        stopTimer();
        setMode('camera');
      }
    } catch (e) {
      console.error('Recording failed:', e);
      stopTimer();
      setMode('camera');
    }
  };

  const stopRecording = () => {
    if (Platform.OS === 'web') {
      try {
        webRecorderRef.current?.stop();
      } catch (e) {
        console.error('Failed to stop web recording:', e);
      }
    } else {
      cameraRef.current?.stopRecording();
    }
  };

  const handleRecordButtonPress = () => {
    if (mode === 'camera') {
      startRecording();
    } else if (mode === 'recording') {
      stopRecording();
    }
  };

  const toggleFacing = () => {
    setFacing((prev) => (prev === 'back' ? 'front' : 'back'));
  };

  const handleCancelPreview = () => {
    setRecordedUri(null);
    setElapsed(0);
    setMode('camera');
  };

  const handleSend = () => {
    if (recordedUri) {
      onSend(recordedUri);
    }
  };

  const handleClose = () => {
    stopTimer();
    if (mode === 'recording') {
      stopRecording();
    }
    onClose();
  };

  if (!visible) return null;

  const permissionsGranted = cameraPermission?.granted && micPermission?.granted;

  return (
    <View style={styles.overlay}>
      <TouchableOpacity style={styles.closeButton} onPress={handleClose}>
        <Text style={styles.closeButtonText}>✕</Text>
      </TouchableOpacity>

      {!permissionsGranted ? (
        <View style={styles.permissionContainer}>
          <Text style={styles.permissionText}>
            Camera and microphone access are required to record a video circle.
          </Text>
        </View>
      ) : mode === 'preview' && recordedUri ? (
        <>
          <View
            style={[
              styles.viewfinder,
              { width: VIEWFINDER_SIZE, height: VIEWFINDER_SIZE, borderRadius: VIEWFINDER_SIZE / 2 },
            ]}
          >
            <Video
              source={{ uri: recordedUri }}
              style={StyleSheet.absoluteFill}
              videoStyle={Platform.select({
                web: { objectFit: 'cover', objectPosition: 'center', width: '100%', height: '100%' } as any,
                default: { width: '100%', height: '100%' },
              })}
              resizeMode={ResizeMode.COVER}
              isMuted={false}
              isLooping
              shouldPlay
              useNativeControls={false}
            />
          </View>
          <View style={styles.previewActions}>
            <TouchableOpacity style={[styles.previewButton, styles.cancelButton]} onPress={handleCancelPreview}>
              <Text style={styles.previewButtonText}>Cancel</Text>
            </TouchableOpacity>
            <TouchableOpacity style={[styles.previewButton, styles.sendButton]} onPress={handleSend}>
              <Text style={styles.previewButtonText}>Send</Text>
            </TouchableOpacity>
          </View>
        </>
      ) : (
        <>
          <TouchableOpacity style={styles.flipButton} onPress={toggleFacing}>
            <Text style={styles.flipButtonText}>⟳</Text>
          </TouchableOpacity>

          <View
            style={[
              styles.viewfinder,
              { width: VIEWFINDER_SIZE, height: VIEWFINDER_SIZE, borderRadius: VIEWFINDER_SIZE / 2 },
            ]}
          >
            <CameraView
              ref={cameraRef}
              style={{ width: VIEWFINDER_SIZE, height: VIEWFINDER_SIZE }}
              facing={facing}
              mode="video"
            />
          </View>

          {mode === 'recording' && (
            <View style={styles.timerBadge}>
              <View style={styles.recordingDot} />
              <Text style={styles.timerText}>{formatTime(elapsed)} / {formatTime(MAX_DURATION)}</Text>
            </View>
          )}

          <View style={styles.progressTrack}>
            <View
              style={[
                styles.progressFill,
                { width: `${Math.min(100, (elapsed / MAX_DURATION) * 100)}%` },
              ]}
            />
          </View>

          <TouchableOpacity
            style={[
              styles.recordButtonOuter,
              mode === 'recording' && styles.recordButtonOuterActive,
            ]}
            onPress={handleRecordButtonPress}
          >
            <View
              style={[
                styles.recordButtonInner,
                mode === 'recording' && styles.recordButtonInnerActive,
              ]}
            />
          </TouchableOpacity>
        </>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  overlay: {
    ...StyleSheet.absoluteFillObject,
    backgroundColor: '#000',
    justifyContent: 'center',
    alignItems: 'center',
    zIndex: 1000,
  },
  closeButton: {
    position: 'absolute',
    top: 48,
    left: 20,
    width: 36,
    height: 36,
    borderRadius: 18,
    backgroundColor: 'rgba(255,255,255,0.15)',
    justifyContent: 'center',
    alignItems: 'center',
  },
  closeButtonText: {
    color: '#fff',
    fontSize: 18,
    fontWeight: '600',
  },
  flipButton: {
    position: 'absolute',
    top: 48,
    right: 20,
    width: 36,
    height: 36,
    borderRadius: 18,
    backgroundColor: 'rgba(255,255,255,0.15)',
    justifyContent: 'center',
    alignItems: 'center',
  },
  flipButtonText: {
    color: '#fff',
    fontSize: 20,
  },
  permissionContainer: {
    paddingHorizontal: 32,
  },
  permissionText: {
    color: '#fff',
    fontSize: 16,
    textAlign: 'center',
  },
  viewfinder: {
    overflow: 'hidden',
    backgroundColor: '#222',
    borderWidth: 3,
    borderColor: '#007AFF',
  },
  timerBadge: {
    position: 'absolute',
    top: 100,
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: 'rgba(0,0,0,0.5)',
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 12,
  },
  recordingDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: '#FF3B30',
    marginRight: 6,
  },
  timerText: {
    color: '#fff',
    fontSize: 13,
    fontWeight: '600',
  },
  progressTrack: {
    position: 'absolute',
    bottom: 130,
    width: VIEWFINDER_SIZE,
    height: 4,
    borderRadius: 2,
    backgroundColor: 'rgba(255,255,255,0.2)',
    overflow: 'hidden',
  },
  progressFill: {
    height: '100%',
    backgroundColor: '#FF3B30',
  },
  recordButtonOuter: {
    position: 'absolute',
    bottom: 48,
    width: 76,
    height: 76,
    borderRadius: 38,
    borderWidth: 4,
    borderColor: '#fff',
    justifyContent: 'center',
    alignItems: 'center',
  },
  recordButtonOuterActive: {
    borderColor: '#FF3B30',
  },
  recordButtonInner: {
    width: 60,
    height: 60,
    borderRadius: 30,
    backgroundColor: '#FF3B30',
  },
  recordButtonInnerActive: {
    width: 28,
    height: 28,
    borderRadius: 6,
  },
  previewActions: {
    position: 'absolute',
    bottom: 48,
    flexDirection: 'row',
    justifyContent: 'center',
    alignItems: 'center',
  },
  previewButton: {
    paddingHorizontal: 28,
    paddingVertical: 12,
    borderRadius: 24,
    marginHorizontal: 10,
  },
  cancelButton: {
    backgroundColor: 'rgba(255,255,255,0.15)',
  },
  sendButton: {
    backgroundColor: '#007AFF',
  },
  previewButtonText: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '600',
  },
});
