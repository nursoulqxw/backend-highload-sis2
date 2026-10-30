"""Run: python -m app.worker"""

import asyncio

from app.worker.sync import run

asyncio.run(run())
