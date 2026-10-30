"""Run: python -m app.grpc_api"""

import asyncio

from app.grpc_api.server import serve

asyncio.run(serve())
