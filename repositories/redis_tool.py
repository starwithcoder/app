import time

import chromadb

from infrastructure.logging.logger import logger
import asyncio
from config.settings import settings
from infrastructure.database.redis_client import redis_client
from infrastructure.tools.local.summary_tool import generate_summary
import redis
from infrastructure.database.chromadb_client import chromadb_client
SCAN_INTERVAL = settings.SCAN_INTERVAL
SESSION_TIMEOUT = settings.SESSION_TIMEOUT


class RedisTool():

    def __init__(self):
        self.r = redis_client

    async def finalize_session(self, user_id: str, session_id: str):
        if not self.r.ping():
            raise redis.exceptions.ConnectionError

        pending_key = f"chat:sess:{user_id}:{session_id}:pending"
        summary_key = f"chat:sess:{user_id}:{session_id}:summary"
        #[{"role":"","message":""}]
        message = await self.r.lrange(pending_key, 0, -1)
        if not message:
            return
        old_summary = self.r.get(summary_key) or ''

        chromadb_client.add_summary(user_id=user_id,session_id=session_id,summary=old_summary)
        chromadb_client.add_memories(user_id=user_id,session_id=session_id,message=message)
        #new_summary = generate_summary(message)
        pipe = self.r.pipeline()
        pipe.set(summary_key, new_summary, ex=86400)
        pipe.ltrim(pending_key, 1, 0)
        pipe.zrem("chat:active_sessions", user_id + ":" + session_id)
        await pipe.execute()

    async def scan_and_finalize(self, user_id: str, session_id: str):
        if not await self.r.set("chat:scan_lock", "1", nx=True, ex=SCAN_INTERVAL - 5):
            return
        now = int(time.time())
        threshold = now - SESSION_TIMEOUT
        expired = await self.r.zrangebyscore("chat:active_sessions", 0, threshold)
        for sid in expired:
            await self.r.zrem("chat:active_sessions", sid)
            await self.finalize_session(user_id, session_id)

    async def periodic_scanner(self, user_id: str, session_id: str):
        while True:
            try:
                await self.scan_and_finalize(user_id, session_id)
            except:
                logger.exception(f"定时扫描失败")
            await asyncio.sleep(SCAN_INTERVAL)
