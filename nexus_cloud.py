#!/usr/bin/env python3
"""
NEXUS Cloud Relay Server
=========================
Deploy this on Railway/Render (free tier).
Each user runs nexus_agent.py on their own laptop.
Browser connects to this cloud server.
Commands are relayed to the correct user's laptop agent.

Install: pip install websockets
Run: python nexus_cloud.py
"""

import asyncio
import json
import os
import time
import hashlib
import secrets
import websockets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import threading

PORT = int(os.environ.get('PORT', 8080))

# ── Connected agents: {agent_id: websocket} ──
agents = {}
# ── Pending responses: {request_id: asyncio.Future} ──
pending = {}
# ── Agent tokens: {token: agent_id} ──
agent_tokens = {}

# ═══════════════════════════════════════════════
#  WEBSOCKET HANDLER — Laptop agents connect here
# ═══════════════════════════════════════════════
async def agent_handler(websocket):
    agent_id = None
    try:
        # First message must be auth
        auth_msg = await asyncio.wait_for(websocket.recv(), timeout=10)
        auth = json.loads(auth_msg)

        if auth.get('type') != 'auth':
            await websocket.send(json.dumps({'type': 'error', 'message': 'Auth required'}))
            return

        token = auth.get('token', '')
        agent_id = auth.get('agent_id', '')

        if not token or not agent_id:
            await websocket.send(json.dumps({'type': 'error', 'message': 'Invalid auth'}))
            return

        # Register agent
        agents[agent_id] = websocket
        agent_tokens[token] = agent_id
        print(f"[CLOUD] Agent connected: {agent_id}")
        await websocket.send(json.dumps({'type': 'auth_ok', 'agent_id': agent_id}))

        # Keep alive — listen for responses
        async for message in websocket:
            try:
                data = json.loads(message)
                req_id = data.get('request_id')
                if req_id and req_id in pending:
                    pending[req_id].set_result(data)
            except:
                pass

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"[CLOUD] Agent error: {e}")
    finally:
        if agent_id and agent_id in agents:
            del agents[agent_id]
            print(f"[CLOUD] Agent disconnected: {agent_id}")


# ═══════════════════════════════════════════════
#  HTTP HANDLER — Browser sends commands here
# ═══════════════════════════════════════════════
class CloudHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]} {args[1]}")

    def send_cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Agent-Token')

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        # CORS
        if parsed.path == '/ping':
            self.send_response(200)
            self.send_cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            connected = list(agents.keys())
            self.wfile.write(json.dumps({
                'status': 'ok',
                'agents': len(connected),
                'os': 'cloud'
            }).encode())
            return

        if parsed.path == '/cmd':
            token = self.headers.get('X-Agent-Token', '') or qs.get('token', [''])[0]
            action = qs.get('action', [''])[0]
            args = {k: v[0] for k, v in qs.items() if k not in ('action', 'token')}

            # Find agent for this token
            agent_id = agent_tokens.get(token)
            if not agent_id or agent_id not in agents:
                self.send_response(503)
                self.send_cors()
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'ok': False,
                    'message': 'Agent not connected. Run nexus_agent.py on your laptop.'
                }).encode())
                return

            # Forward command to agent
            req_id = secrets.token_hex(8)
            loop = asyncio.new_event_loop()

            async def send_cmd():
                future = loop.create_future()
                pending[req_id] = future
                ws = agents[agent_id]
                await ws.send(json.dumps({
                    'type': 'cmd',
                    'action': action,
                    'args': args,
                    'request_id': req_id
                }))
                try:
                    result = await asyncio.wait_for(future, timeout=15)
                    return result
                except asyncio.TimeoutError:
                    return {'ok': False, 'message': 'Timeout'}
                finally:
                    pending.pop(req_id, None)

            try:
                result = loop.run_until_complete(send_cmd())
            except Exception as e:
                result = {'ok': False, 'message': str(e)}
            finally:
                loop.close()

            self.send_response(200)
            self.send_cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return

        if parsed.path == '/register':
            # Generate new token for a new user
            token = secrets.token_hex(16)
            agent_id = 'agent_' + secrets.token_hex(4)
            self.send_response(200)
            self.send_cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'token': token,
                'agent_id': agent_id,
                'ws_url': f'wss://{self.headers.get("Host", "localhost")}/ws'
            }).encode())
            return

        # Serve main page
        if parsed.path == '/' or parsed.path == '/index.html':
            self.send_response(200)
            self.send_cors()
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            html = """<!DOCTYPE html>
<html><head><title>NEXUS Cloud</title>
<style>body{background:#0a0a0a;color:#00ffcc;font-family:monospace;text-align:center;padding:40px}
h1{font-size:3em;letter-spacing:10px}
.status{background:#111;padding:20px;border-radius:10px;margin:20px auto;max-width:400px}
a{color:#00ffcc;font-size:1.2em}</style></head>
<body>
<h1>NEXUS</h1>
<div class="status">
<p>🌐 Cloud Relay Server Online</p>
<p id="agents">Checking agents...</p>
</div>
<p><a href="/register">Get Your Token</a></p>
<script>
fetch('/ping').then(r=>r.json()).then(d=>{
  document.getElementById('agents').textContent = d.agents + ' agent(s) connected';
});
</script>
</body></html>"""
            self.wfile.write(html.encode())
            return

        self.send_response(404)
        self.end_headers()


# ═══════════════════════════════════════════════
#  WEBSOCKET SERVER — Run alongside HTTP
# ═══════════════════════════════════════════════
def run_websocket_server():
    async def main():
        ws_port = PORT + 1
        print(f"[CLOUD] WebSocket server on port {ws_port}")
        async with websockets.serve(agent_handler, '0.0.0.0', ws_port):
            await asyncio.Future()  # run forever
    asyncio.run(main())


if __name__ == '__main__':
    print(f"""
╔══════════════════════════════════════╗
║       NEXUS Cloud Relay Server       ║
║  HTTP Port: {PORT:<26}║
║  WS Port:   {PORT+1:<26}║
╚══════════════════════════════════════╝
""")
    # Start WebSocket server in thread
    ws_thread = threading.Thread(target=run_websocket_server, daemon=True)
    ws_thread.start()

    # Start HTTP server
    server = HTTPServer(('0.0.0.0', PORT), CloudHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[CLOUD] Server stopped.")
