"""MongoDB Atlas async client (PyMongo 4.13+ AsyncMongoClient).

One AsyncMongoClient is kept per running event loop. PyMongo handles pooling
and reconnects. Call aclose() at the end of a job to release the pool.

AsyncMongoClient instances cannot be shared across event loops. On Linux each
LiveKit job runs in its own process, but on Windows the default job executor
is a thread, so several jobs share this module while each runs its own loop.
Keying the cache by loop keeps every job on a client bound to its own loop.
"""

import asyncio
import os
import weakref

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

DEFAULT_DB_NAME = "livekit_mongo_starter"

_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncMongoClient] = (
    weakref.WeakKeyDictionary()
)


async def get_mongo_client() -> AsyncMongoClient:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        uri = os.getenv("MONGODB_URI")
        if not uri:
            raise RuntimeError("MONGODB_URI environment variable is not set.")
        client = AsyncMongoClient(uri)
        _clients[loop] = client
    return client


async def get_db(db_name: str | None = None) -> AsyncDatabase:
    client = await get_mongo_client()
    # `or` (not a getenv default) so an empty `MONGODB_DB=` line in .env.local
    # still falls back to the default name.
    return client[db_name or os.getenv("MONGODB_DB") or DEFAULT_DB_NAME]


async def aclose() -> None:
    """Close the client that belongs to the current event loop, if any."""
    client = _clients.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.close()
