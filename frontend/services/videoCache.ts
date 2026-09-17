import { Platform } from 'react-native';
import { API_BASE_URL } from './config';

let FileSystem: any = null;
if (Platform.OS !== 'web') {
  FileSystem = require('expo-file-system');
}

const CACHE_DIR = FileSystem ? `${FileSystem.cacheDirectory}video-circles/` : '';
const inflight = new Map<string, Promise<string>>();

function urlToFilename(url: string): string {
  let hash = 0;
  for (let i = 0; i < url.length; i++) {
    hash = ((hash << 5) - hash + url.charCodeAt(i)) | 0;
  }
  const ext = url.split('.').pop()?.split('?')[0] || 'mp4';
  return `${Math.abs(hash).toString(36)}.${ext}`;
}

function resolveUrl(path: string): string {
  if (path.startsWith('http://') || path.startsWith('https://')) return path;
  return `${API_BASE_URL}${path.startsWith('/') ? '' : '/'}${path}`;
}

async function ensureCacheDir(): Promise<void> {
  if (!FileSystem) return;
  const info = await FileSystem.getInfoAsync(CACHE_DIR);
  if (!info.exists) {
    await FileSystem.makeDirectoryAsync(CACHE_DIR, { intermediates: true });
  }
}

export async function getCachedVideoUri(serverPath: string): Promise<string> {
  const fullUrl = resolveUrl(serverPath);

  if (Platform.OS === 'web' || !FileSystem) {
    return fullUrl;
  }

  const existing = inflight.get(fullUrl);
  if (existing) return existing;

  const localUri = `${CACHE_DIR}${urlToFilename(fullUrl)}`;

  const promise = (async () => {
    try {
      await ensureCacheDir();
      const info = await FileSystem.getInfoAsync(localUri);
      if (info.exists) return localUri;
      const result = await FileSystem.downloadAsync(fullUrl, localUri);
      return result.uri;
    } catch (e) {
      console.error('Video cache miss, falling back to remote:', e);
      inflight.delete(fullUrl);
      return fullUrl;
    }
  })();

  inflight.set(fullUrl, promise);
  return promise;
}
