import asyncio
import websockets

async def test():
    try:
        async with websockets.connect('ws://127.0.0.1:8765') as ws:
            print("Connected to 127.0.0.1:8765")
    except Exception as e:
        print("127.0.0.1 failed:", e)

    try:
        async with websockets.connect('ws://localhost:8765') as ws:
            print("Connected to localhost:8765")
    except Exception as e:
        print("localhost failed:", e)

asyncio.run(test())
