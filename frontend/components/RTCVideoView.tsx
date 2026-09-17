import { useEffect, useRef } from 'react';

export default function RTCVideoView({
  stream,
  style,
  mirror,
  muted,
}: {
  stream: MediaStream | null;
  style?: any;
  mirror?: boolean;
  muted?: boolean;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (videoRef.current && stream) {
      videoRef.current.srcObject = stream;
    }
  }, [stream]);

  return (
    <video
      ref={videoRef}
      autoPlay
      playsInline
      muted={muted}
      style={{
        ...style,
        transform: mirror ? 'scaleX(-1)' : undefined,
        objectFit: 'cover',
      }}
    />
  );
}
