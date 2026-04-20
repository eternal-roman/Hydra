import asyncio
import websockets
import json

async def test():
    print("Connecting...")
    async with websockets.connect('ws://localhost:8765') as ws:
        print('connected')
        try:
            msg = await ws.recv()
            print('received', len(msg))
            await asyncio.sleep(2)
            print('done')
        except Exception as e:
            print("Error", type(e), e)

asyncio.run(test())
