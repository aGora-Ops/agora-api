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

def _clean_redis_url() -> tuple[str, dict]:
    """Strip ssl_cert_reqs from the URL (redis-py rejects the string value)
    and return it as the proper ssl.CERT_NONE integer kwarg instead."""
    url = settings.REDIS_URL
    if not url.startswith("rediss://"):
        return url, {}
    import ssl
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs.pop("ssl_cert_reqs", None)
    clean = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
    return clean, {"ssl_cert_reqs": ssl.CERT_NONE}


async def redis_event_listener() -> None:
    """Subscribe to the agora:events Redis channel and broadcast to WS clients."""
    while True:
        try:
            url, ssl_kwargs = _clean_redis_url()
            redis = await Redis.from_url(url, decode_responses=True, **ssl_kwargs)
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
