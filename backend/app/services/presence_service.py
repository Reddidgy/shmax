from datetime import datetime, timedelta
from typing import Optional
import redis.asyncio as redis
from app.config import settings

redis_client: Optional[redis.Redis] = None


async def get_redis():
    """Get Redis client instance"""
    global redis_client
    if redis_client is None:
        redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return redis_client


async def close_redis():
    """Close Redis connection"""
    global redis_client
    if redis_client:
        await redis_client.close()


async def set_online(user_id: str):
    """Mark user as online"""
    try:
        client = await get_redis()
        await client.set(f"presence:{user_id}", "online", ex=300)
    except Exception:
        pass


async def set_offline(user_id: str):
    """Mark user as offline and set last_seen"""
    try:
        client = await get_redis()
        await client.delete(f"presence:{user_id}")
        await client.set(f"last_seen:{user_id}", datetime.utcnow().isoformat(), ex=86400 * 30)
    except Exception:
        pass


async def is_online(user_id: str) -> bool:
    """Check if user is online"""
    try:
        client = await get_redis()
        result = await client.get(f"presence:{user_id}")
        return result == "online"
    except Exception:
        return False


async def get_last_seen(user_id: str) -> Optional[datetime]:
    """Get user's last seen timestamp"""
    try:
        client = await get_redis()
        result = await client.get(f"last_seen:{user_id}")
        if result:
            return datetime.fromisoformat(result)
    except Exception:
        pass
    return None
