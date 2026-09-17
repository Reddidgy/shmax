import { View, Text, StyleSheet } from 'react-native';
import VideoCircleBubble from './VideoCircleBubble';

interface MessageBubbleProps {
  message: {
    id: string;
    sender: {
      id: string;
      display_name: string;
    };
    content?: string;
    message_type: string;
    media_url?: string;
    thumbnail_url?: string;
    state: string;
    created_at: string;
  };
  isOwn: boolean;
  showSender: boolean;
}

export default function MessageBubble({
  message,
  isOwn,
  showSender,
}: MessageBubbleProps) {
  const formatTime = (dateString: string) => {
    const date = new Date(dateString);
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  const getStateIcon = (state: string) => {
    switch (state) {
      case 'sent':
        return '✓';
      case 'delivered':
        return '✓✓';
      case 'read':
        return '✓✓';
      default:
        return '';
    }
  };

  if (message.message_type === 'video_circle') {
    return (
      <View
        style={[
          styles.container,
          isOwn ? styles.ownContainer : styles.otherContainer,
        ]}
      >
        {!isOwn && showSender && (
          <Text style={styles.senderName}>{message.sender.display_name}</Text>
        )}
        <VideoCircleBubble
          mediaUrl={message.media_url!}
          thumbnailUrl={message.thumbnail_url}
        />
        <View style={styles.videoCircleFooter}>
          <Text style={styles.videoCircleTime}>{formatTime(message.created_at)}</Text>
          {isOwn && (
            <Text style={styles.stateIcon}>{getStateIcon(message.state)}</Text>
          )}
        </View>
      </View>
    );
  }

  return (
    <View
      style={[
        styles.container,
        isOwn ? styles.ownContainer : styles.otherContainer,
      ]}
    >
      {!isOwn && showSender && (
        <Text style={styles.senderName}>{message.sender.display_name}</Text>
      )}
      <View
        style={[
          styles.bubble,
          isOwn ? styles.ownBubble : styles.otherBubble,
        ]}
      >
        <Text style={[styles.content, isOwn ? styles.ownContent : styles.otherContent]}>
          {message.content}
        </Text>
        <View style={styles.footer}>
          <Text style={[styles.time, isOwn ? styles.ownTime : styles.otherTime]}>
            {formatTime(message.created_at)}
          </Text>
          {isOwn && (
            <Text style={styles.stateIcon}>{getStateIcon(message.state)}</Text>
          )}
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    marginVertical: 4,
    maxWidth: '80%',
  },
  ownContainer: {
    alignSelf: 'flex-end',
  },
  otherContainer: {
    alignSelf: 'flex-start',
  },
  senderName: {
    fontSize: 12,
    color: '#666',
    marginBottom: 2,
    marginLeft: 12,
  },
  bubble: {
    padding: 12,
    borderRadius: 16,
  },
  ownBubble: {
    backgroundColor: '#007AFF',
    borderBottomRightRadius: 4,
  },
  otherBubble: {
    backgroundColor: '#E9E9EB',
    borderBottomLeftRadius: 4,
  },
  content: {
    fontSize: 16,
    lineHeight: 20,
  },
  ownContent: {
    color: '#fff',
  },
  otherContent: {
    color: '#000',
  },
  footer: {
    flexDirection: 'row',
    alignItems: 'center',
    marginTop: 4,
  },
  time: {
    fontSize: 11,
    marginRight: 4,
  },
  ownTime: {
    color: 'rgba(255, 255, 255, 0.7)',
  },
  otherTime: {
    color: '#666',
  },
  stateIcon: {
    fontSize: 11,
    color: 'rgba(255, 255, 255, 0.7)',
  },
  videoCircleFooter: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    marginTop: 4,
  },
  videoCircleTime: {
    fontSize: 11,
    color: '#666',
    marginRight: 4,
  },
});
