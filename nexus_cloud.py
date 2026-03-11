#!/usr/bin/env python3
"""
NEXUS Cloud Relay Server
=========================
Single port - handles both HTTP and WebSocket.
Deploy on Railway/Render free tier.

Install: pip install aiohttp
Run: python nexus_cloud.py
"""

import asyncio
import json
import os
import secrets
from aiohttp import web
import aiohttp

PORT = int(os.environ.get('PORT', 8080))

# ── State ──
agents = {}        # {token: ws}
pending = {}       # {request_id: Future}

# ═══════════════════════════════════════════════
#  WebSocket — Agents connect here
# ═══════════════════════════════════════════════
async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    token = None
    try:
        # First message = auth
        msg = await asyncio.wait_for(ws.__anext__(), timeout=10)
        if msg.type != aiohttp.WSMsgType.TEXT:
            return ws

        data = json.loads(msg.data)
        if data.get('type') != 'auth':
            await ws.send_str(json.dumps({'type': 'error', 'message': 'Auth required'}))
            return ws

        token = data.get('token', '')
        agent_id = data.get('agent_id', 'unknown')

        if not token:
            await ws.send_str(json.dumps({'type': 'error', 'message': 'Token required'}))
            return ws

        agents[token] = ws
        print(f"[CLOUD] Agent connected: {agent_id} token={token[:8]}...")
        await ws.send_str(json.dumps({'type': 'auth_ok', 'agent_id': agent_id}))

        # Listen for command responses
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    resp = json.loads(msg.data)
                    req_id = resp.get('request_id')
                    if req_id and req_id in pending:
                        pending[req_id].set_result(resp)
                except Exception as e:
                    print(f"[CLOUD] Parse error: {e}")
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    except asyncio.TimeoutError:
        print("[CLOUD] Agent auth timeout")
    except Exception as e:
        print(f"[CLOUD] WS error: {e}")
    finally:
        if token and token in agents:
            del agents[token]
            print(f"[CLOUD] Agent disconnected: {token[:8]}...")

    return ws


# ═══════════════════════════════════════════════
#  HTTP — Browser sends commands here
# ═══════════════════════════════════════════════
async def ping_handler(request):
    return web.json_response({
        'status': 'ok',
        'agents': len(agents),
        'os': 'cloud'
    }, headers={'Access-Control-Allow-Origin': '*'})


async def cmd_handler(request):
    headers = {'Access-Control-Allow-Origin': '*'}

    # Get token
    token = request.headers.get('X-Agent-Token', '') or request.rel_url.query.get('token', '')
    action = request.rel_url.query.get('action', '')
    args = {k: v for k, v in request.rel_url.query.items() if k not in ('action', 'token')}

    if not token or token not in agents:
        return web.json_response({
            'ok': False,
            'message': 'Agent not connected. Run nexus_agent.py on your laptop.'
        }, status=503, headers=headers)

    # Forward to agent
    req_id = secrets.token_hex(8)
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    pending[req_id] = future

    ws = agents[token]
    await ws.send_str(json.dumps({
        'type': 'cmd',
        'action': action,
        'args': args,
        'request_id': req_id
    }))

    try:
        result = await asyncio.wait_for(future, timeout=15)
    except asyncio.TimeoutError:
        result = {'ok': False, 'message': 'Timeout — agent did not respond'}
    finally:
        pending.pop(req_id, None)

    return web.json_response(result, headers=headers)


async def options_handler(request):
    return web.Response(headers={
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type, X-Agent-Token'
    })


async def index_handler(request):
    html = """<!DOCTYPE html>
<html><head><title>NEXUS Cloud</title>
<style>
  body{background:#0a0a0a;color:#00ffcc;font-family:monospace;text-align:center;padding:40px}
  h1{font-size:3em;letter-spacing:10px;margin-bottom:5px}
  .box{background:#111;padding:20px;border-radius:10px;margin:20px auto;max-width:450px;border:1px solid #00ffcc33}
  a{color:#00ffcc;font-size:1.1em;text-decoration:none;border:1px solid #00ffcc;padding:8px 20px;border-radius:5px}
  a:hover{background:#00ffcc22}
  .dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:#00ffcc;margin-right:8px}
</style></head>
<body>
<h1>NEXUS</h1>
<p style="color:#888;letter-spacing:3px">CLOUD RELAY SERVER</p>
<div class="box">
  <p><span class="dot"></span>Server Online</p>
  <p id="agents">Checking agents...</p>
</div>
<br>
<a href="/register">Get Your Token</a>
<script>
fetch('/ping').then(r=>r.json()).then(d=>{
  document.getElementById('agents').textContent = d.agents + ' agent(s) connected';
}).catch(()=>{
  document.getElementById('agents').textContent = 'Error checking agents';
});
</script>
</body></html>"""
    return web.Response(text=html, content_type='text/html')


async def register_handler(request):
    token = secrets.token_hex(16)
    agent_id = 'agent_' + secrets.token_hex(4)
    host = request.headers.get('Host', 'localhost')
    return web.json_response({
        'token': token,
        'agent_id': agent_id,
        'ws_url': f'wss://{host}/ws',
        'browser_url': f'https://{host}?token={token}'
    }, headers={'Access-Control-Allow-Origin': '*'})


# ═══════════════════════════════════════════════
#  APP SETUP
# ═══════════════════════════════════════════════
app = web.Application()
app.router.add_get('/ws', ws_handler)
app.router.add_get('/ping', ping_handler)
app.router.add_get('/cmd', cmd_handler)
app.router.add_post('/cmd', cmd_handler)
app.router.add_options('/cmd', options_handler)
app.router.add_get('/register', register_handler)
app.router.add_get('/', index_handler)

if __name__ == '__main__':
    print(f"""
╔══════════════════════════════════════╗
║     NEXUS Cloud Relay Server         ║
║     Port: {PORT:<27}║
╚══════════════════════════════════════╝
""")
    web.run_app(app, host='0.0.0.0', port=PORT)
