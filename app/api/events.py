"""Redis pub/sub listener that relays worker events to WebSocket clients.

The remediation-worker publishes events (run_update, remediation_created,
remediation_updated) to the Redis channel ``agora:events`` after each DB
commit. This background task subscribes to that channel and calls
``manager.broadcast()`` so connected dashboard clients receive live updates
without polling.
"""
import asyncio
import json
import logging

from redis.asyncio import Redis

from app.core.config import settings
from app.api.v1.routes.websocket import manager

logger = logging.getLogger(__name__)

REDIS_CHANNEL = "agora:events"

def _redis_ssl_kwargs() -> dict:
    if settings.REDIS_URL.startswith("rediss://"):
        import ssl
        return {"ssl_cert_reqs": ssl.CERT_NONE}
    return {}


async def redis_event_listener() -> None:
    """Subscribe to the agora:events Redis channel and broadcast to WS clients."""
    while True:
        try:
            redis = await Redis.from_url(
                settings.REDIS_URL, decode_responses=True, **_redis_ssl_kwargs()
            )
            pubsub = redis.pubsub()
            await pubsub.subscribe(REDIS_CHANNEL)
            logger.info("Subscribed to Redis channel %s", REDIS_CHANNEL)

            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    await manager.broadcast(data)
                except Exception as exc:
                    logger.exception("Failed to broadcast WebSocket event: %s", exc)

        except asyncio.CancelledError:
            logger.info("Redis event listener cancelled")
            return
        except Exception as exc:
            logger.warning("Redis pub/sub disconnected, reconnecting in 5s: %s", exc)
            await asyncio.sleep(5)
