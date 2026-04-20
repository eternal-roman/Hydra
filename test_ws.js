const WebSocket = require('ws');

const ws1 = new WebSocket('ws://127.0.0.1:8765');
ws1.on('open', () => {
    console.log('Connected to 127.0.0.1:8765 successfully!');
    ws1.close();
});
ws1.on('error', (e) => console.log('127.0.0.1 error:', e.message));

const ws2 = new WebSocket('ws://localhost:8765');
ws2.on('open', () => {
    console.log('Connected to localhost:8765 successfully!');
    ws2.close();
});
ws2.on('error', (e) => console.log('localhost error:', e.message));
