import { RTCView } from 'react-native-webrtc';

export default function RTCVideoView({
  stream,
  style,
  mirror,
}: {
  stream: any;
  style?: any;
  mirror?: boolean;
}) {
  return (
    <RTCView
      streamURL={stream?.toURL?.()}
      style={style}
      objectFit="cover"
      mirror={mirror}
    />
  );
}
