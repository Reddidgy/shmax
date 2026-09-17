import { useState, useRef, useEffect } from 'react';
import {
  View,
  TouchableOpacity,
  StyleSheet,
  Image,
  Text,
  Animated,
  Platform,
} from 'react-native';
import { Video, ResizeMode, AVPlaybackStatus } from 'expo-av';
import { API_BASE_URL } from '../services/config';
import { getCachedVideoUri } from '../services/videoCache';

interface VideoCircleBubbleProps {
  mediaUrl: string;
  thumbnailUrl?: string;
  size?: number;
}

function resolveUrl(path: string): string {
  if (!path) return path;
  if (path.startsWith('http://') || path.startsWith('https://')) {
    return path;
  }
  return `${API_BASE_URL}${path.startsWith('/') ? '' : '/'}${path}`;
}

function formatTime(seconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(seconds));
  const mins = Math.floor(totalSeconds / 60);
  const secs = totalSeconds % 60;
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

export default function VideoCircleBubble({
  mediaUrl,
  thumbnailUrl,
  size = 200,
}: VideoCircleBubbleProps) {
  const [isPlaying, setIsPlaying] = useState(false);
  const [isMuted, setIsMuted] = useState(true);
  const [durationMillis, setDurationMillis] = useState(0);
  const [positionMillis, setPositionMillis] = useState(0);
  const [cachedUri, setCachedUri] = useState<string | null>(null);
  const videoRef = useRef<Video>(null);
  const progressAnim = useRef(new Animated.Value(0)).current;

  const resolvedThumbnailUrl = thumbnailUrl ? resolveUrl(thumbnailUrl) : undefined;

  useEffect(() => {
    getCachedVideoUri(mediaUrl).then(setCachedUri);
  }, [mediaUrl]);

  useEffect(() => {
    const progress = durationMillis > 0 ? positionMillis / durationMillis : 0;
    Animated.timing(progressAnim, {
      toValue: progress,
      duration: 150,
      useNativeDriver: false,
    }).start();
  }, [positionMillis, durationMillis]);

  const handlePress = async () => {
    if (!isPlaying) {
      setIsMuted(false);
      setIsPlaying(true);
    } else {
      setIsPlaying(false);
      await videoRef.current?.pauseAsync();
    }
  };

  const handlePlaybackStatusUpdate = (status: AVPlaybackStatus) => {
    if (!status.isLoaded) return;
    if (status.durationMillis) {
      setDurationMillis(status.durationMillis);
    }
    setPositionMillis(status.positionMillis || 0);
    if (status.didJustFinish) {
      setIsPlaying(false);
      setPositionMillis(0);
      videoRef.current?.setPositionAsync(0);
    }
  };

  const secondsElapsed = isPlaying
    ? positionMillis / 1000
    : durationMillis
    ? durationMillis / 1000
    : 0;

  const barWidth = progressAnim.interpolate({
    inputRange: [0, 1],
    outputRange: ['0%', '100%'],
  });

  return (
    <TouchableOpacity
      activeOpacity={0.9}
      onPress={handlePress}
      style={[
        styles.container,
        { width: size, height: size, borderRadius: size / 2 },
      ]}
    >
      {isPlaying && cachedUri ? (
        <Video
          ref={videoRef}
          source={{ uri: cachedUri }}
          style={StyleSheet.absoluteFill}
          videoStyle={Platform.select({
            web: { objectFit: 'cover', objectPosition: 'center', width: '100%', height: '100%' } as any,
            default: { width: '100%', height: '100%' },
          })}
          resizeMode={ResizeMode.COVER}
          isMuted={isMuted}
          shouldPlay={isPlaying}
          useNativeControls={false}
          onPlaybackStatusUpdate={handlePlaybackStatusUpdate}
        />
      ) : resolvedThumbnailUrl ? (
        <Image
          source={{ uri: resolvedThumbnailUrl }}
          style={StyleSheet.absoluteFill}
          resizeMode="cover"
        />
      ) : (
        <View style={[styles.placeholder, StyleSheet.absoluteFill]} />
      )}

      {!isPlaying && (
        <View style={styles.playOverlay}>
          <View style={styles.playIconWrapper}>
            <View style={styles.playIcon} />
          </View>
        </View>
      )}

      <View style={styles.progressTrack}>
        <Animated.View style={[styles.progressBar, { width: barWidth }]} />
      </View>

      <View style={styles.timeBadge}>
        <Text style={styles.timeText}>{formatTime(secondsElapsed)}</Text>
      </View>
    </TouchableOpacity>
  );
}

const styles = StyleSheet.create({
  container: {
    overflow: 'hidden',
    backgroundColor: '#000',
    justifyContent: 'center',
    alignItems: 'center',
    ...Platform.select({
      web: { cursor: 'pointer' as any },
      default: {},
    }),
  },
  placeholder: {
    backgroundColor: '#333',
  },
  playOverlay: {
    ...StyleSheet.absoluteFillObject,
    justifyContent: 'center',
    alignItems: 'center',
    backgroundColor: 'rgba(0,0,0,0.15)',
  },
  playIconWrapper: {
    width: 56,
    height: 56,
    borderRadius: 28,
    backgroundColor: 'rgba(255,255,255,0.85)',
    justifyContent: 'center',
    alignItems: 'center',
  },
  playIcon: {
    width: 0,
    height: 0,
    marginLeft: 4,
    borderTopWidth: 12,
    borderBottomWidth: 12,
    borderLeftWidth: 18,
    borderTopColor: 'transparent',
    borderBottomColor: 'transparent',
    borderLeftColor: '#007AFF',
  },
  progressTrack: {
    position: 'absolute',
    bottom: 28,
    left: 24,
    right: 24,
    height: 3,
    borderRadius: 2,
    backgroundColor: 'rgba(255,255,255,0.35)',
    overflow: 'hidden',
  },
  progressBar: {
    height: '100%',
    backgroundColor: '#fff',
  },
  timeBadge: {
    position: 'absolute',
    bottom: 10,
    alignSelf: 'center',
    paddingHorizontal: 6,
    paddingVertical: 1,
    borderRadius: 8,
    backgroundColor: 'rgba(0,0,0,0.4)',
  },
  timeText: {
    color: '#fff',
    fontSize: 11,
    fontWeight: '600',
  },
});
