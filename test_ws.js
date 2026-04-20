const ws = new WebSocket("ws://localhost:8765");
console.log("Input:", "ws://localhost:8765");
console.log("ws.url:", ws.url);
ws.close();
