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

def _make_redis(decode_responses: bool = False) -> Redis:
    """Build a Redis client that handles ElastiCache TLS correctly.

    redis-py 5.x does not parse ssl_cert_reqs from the URL query string.
    The only working path for CERT_NONE is ConnectionPool.from_url with
    connection_class=SSLConnection and ssl_cert_reqs='none' as a kwarg.
    """
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
    from redis.asyncio.connection import ConnectionPool, SSLConnection

    url = settings.REDIS_URL
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs.pop("ssl_cert_reqs", None)
    clean_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

    if clean_url.startswith("rediss://"):
        pool = ConnectionPool.from_url(
            clean_url,
            connection_class=SSLConnection,
            ssl_cert_reqs="none",
            decode_responses=decode_responses,
        )
    else:
        pool = ConnectionPool.from_url(clean_url, decode_responses=decode_responses)

    return Redis(connection_pool=pool)


async def redis_event_listener() -> None:
    """Subscribe to the agora:events Redis channel and broadcast to WS clients."""
    while True:
        try:
            redis = _make_redis(decode_responses=True)
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
