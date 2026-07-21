import asyncio
import logging
import uuid
import re
import json
from typing import Dict, Any, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, status
from fastapi.responses import HTMLResponse
import uvicorn

logger = logging.getLogger("raytunnel.server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# In-memory storage for active tunnels
# Structure:
# {
#     "subdomain": {
#         "control_ws": WebSocket,
#         "ssh_port": int,
#         "ssh_server": asyncio.Server,
#         "pending_channels": {
#              "channel_id": asyncio.Future (resolves to WebSocket)
#         }
#     }
# }
active_tunnels: Dict[str, Dict[str, Any]] = {}
used_ssh_ports = set()

# Config
SSH_PORT_START = 2200
SSH_PORT_END = 2300
TOKEN = "secret-tunnel-token" # User can override via CLI/env
DOMAIN = "s.yourdomain.com"   # Main domain of the server

app = FastAPI(title="Raytunnel Server")

def get_subdomain_from_host(host: str) -> Optional[str]:
    """Extracts subdomain label from the Host header."""
    host_clean = host.split(":")[0].strip().lower()
    
    # Check if host matches base domains
    root_domain = DOMAIN.lower()
    if root_domain.startswith("s."):
        root_domain = root_domain[2:] # Strip 's.' prefix to get root domain
        
    base_domains = (DOMAIN.lower(), root_domain, "localhost", "127.0.0.1")
    if host_clean in base_domains:
        return None
        
    # Check if host matches <subdomain>.s.yourdomain.com or <subdomain>.yourdomain.com
    for suffix in (f".{DOMAIN.lower()}", f".{root_domain}"):
        if host_clean.endswith(suffix):
            sub = host_clean[:-len(suffix)]
            if "." not in sub: # single level subdomain
                return sub
            # If multi-level, take the leftmost part
            return sub.split(".")[0]
            
    # Fallback: take the first part of the hostname
    parts = host_clean.split(".")
    if len(parts) > 1:
        return parts[0]
    return None


async def allocate_ssh_port() -> int:
    """Finds an available TCP port in the configured SSH port range."""
    for port in range(SSH_PORT_START, SSH_PORT_END):
        if port in used_ssh_ports:
            continue
        # Verify port is actually free by trying to listen on it
        try:
            server = await asyncio.start_server(lambda r, w: w.close(), "0.0.0.0", port)
            server.close()
            await server.wait_closed()
            used_ssh_ports.add(port)
            return port
        except OSError:
            continue
    raise RuntimeError("No available SSH ports in the configured range.")


async def pipe_websocket_tcp(ws: WebSocket, tcp_reader: asyncio.StreamReader, tcp_writer: asyncio.StreamWriter, initial_data: bytes = b""):
    """Pipes bytes bidirectionally between a data WebSocket and a TCP socket."""
    async def tcp_to_ws():
        try:
            if initial_data:
                # If there's any data we already read from TCP, send it first
                await ws.send_bytes(initial_data)
            while True:
                data = await tcp_reader.read(4096)
                if not data:
                    break
                await ws.send_bytes(data)
        except Exception as e:
            logger.debug(f"TCP to WS forward error: {e}")

    async def ws_to_tcp():
        try:
            while True:
                # Receive binary messages
                message = await ws.receive_bytes()
                if not message:
                    break
                tcp_writer.write(message)
                await tcp_writer.drain()
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug(f"WS to TCP forward error: {e}")

    t1 = asyncio.create_task(tcp_to_ws())
    t2 = asyncio.create_task(ws_to_tcp())
    try:
        done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        if t2 not in done:
            # Client finished sending request (t1 done), wait for remote response (t2)
            await t2
    finally:
        t1.cancel()
        t2.cancel()
        tcp_writer.close()
        try:
            await tcp_writer.wait_closed()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


@app.get("/")
def read_root():
    # Return HTML dashboard showing active tunnels
    tunnels_html = ""
    for sub, info in active_tunnels.items():
        tunnels_html += f"<li><strong>{sub}.{DOMAIN}</strong> -> SSH Port: {info['ssh_port']}</li>"
    if not tunnels_html:
        tunnels_html = "<li>No active tunnels.</li>"

    html = f"""
    <html>
        <head>
            <title>Raytunnel Server Dashboard</title>
            <style>
                body {{ font-family: sans-serif; margin: 40px; background-color: #f7f9fa; color: #333; }}
                h1 {{ color: #1a73e8; }}
                ul {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); list-style-type: none; }}
                li {{ padding: 10px 0; border-bottom: 1px solid #eee; }}
                li:last-child {{ border-bottom: none; }}
            </style>
        </head>
        <body>
            <h1>Raytunnel Dashboard</h1>
            <p>Active tunnels:</p>
            <ul>{tunnels_html}</ul>
        </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.websocket("/ws/control")
async def ws_control(websocket: WebSocket, token: str, subdomain: Optional[str] = None):
    """
    Control channel for the tunnel client. Authenticates the client, allocates
    resources, and keeps the channel alive.
    """
    if token != TOKEN:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token")
        return

    await websocket.accept()

    # Generate or validate subdomain
    if not subdomain:
        subdomain = f"client-{uuid.uuid4().hex[:6]}"
    else:
        # Sanitize subdomain
        subdomain = re.sub(r"[^a-zA-Z0-9-]", "", subdomain).lower()
        if not subdomain:
            subdomain = f"client-{uuid.uuid4().hex[:6]}"

    # Handle collisions: evict old session if it exists to prevent subdomain drift on reconnect
    if subdomain in active_tunnels:
        logger.info(f"Subdomain '{subdomain}' is already active. Evicting stale session.")
        old_info = active_tunnels.pop(subdomain, None)
        if old_info:
            # Cancel all pending channel futures so their coroutines can exit cleanly
            for ch in old_info.get("pending_channels", {}).values():
                fut = ch.get("close_fut")
                if fut and not fut.done():
                    fut.cancel()
                try:
                    ch["tcp_writer"].close()
                except Exception:
                    pass
            try:
                await old_info["control_ws"].close(code=status.WS_1001_GOING_AWAY, reason="Evicted by newer session")
            except Exception:
                pass
            try:
                old_info["ssh_server"].close()
                await old_info["ssh_server"].wait_closed()
            except Exception:
                pass
            used_ssh_ports.discard(old_info["ssh_port"])

    try:
        # Allocate SSH Port
        ssh_port = await allocate_ssh_port()
    except Exception as e:
        logger.error(f"Failed to allocate SSH port: {e}")
        await websocket.send_json({"status": "error", "message": "No port available on server"})
        await websocket.close()
        return

    # Create dynamic SSH TCP Server
    async def handle_ssh_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        channel_id = str(uuid.uuid4())
        logger.info(f"New SSH connection on port {ssh_port}. Registering channel {channel_id}")
        
        loop = asyncio.get_running_loop()
        close_fut = loop.create_future()
        connected_fut = loop.create_future()
        active_tunnels[subdomain]["pending_channels"][channel_id] = {
            "tcp_reader": reader,
            "tcp_writer": writer,
            "initial_data": b"",
            "close_fut": close_fut,
            "connected_fut": connected_fut
        }
        
        try:
            # Signal client to open SSH channel — guard against closed/evicted WS
            info = active_tunnels.get(subdomain)
            if not info or info.get("session_id") != session_id:
                logger.debug(f"Session {session_id} for '{subdomain}' already evicted; dropping SSH channel")
                writer.close()
                return
            await websocket.send_json({
                "action": "open_channel",
                "channel_id": channel_id,
                "type": "ssh"
            })
            
            # Wait for client to connect to ws/data (handshake) within 60 seconds
            try:
                await asyncio.wait_for(connected_fut, timeout=60.0)
            except asyncio.TimeoutError:
                logger.warning(f"SSH handshake timed out for channel {channel_id}")
                writer.close()
                return

            # Once connected, wait indefinitely for the SSH connection to close
            try:
                await close_fut
            except asyncio.CancelledError:
                pass
        except Exception as err:
            logger.error(f"Error handling SSH connection: {err}")
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        finally:
            active_tunnels[subdomain]["pending_channels"].pop(channel_id, None)

    try:
        ssh_server = await asyncio.start_server(handle_ssh_conn, "0.0.0.0", ssh_port)
    except Exception as e:
        logger.error(f"Failed to start SSH server on port {ssh_port}: {e}")
        used_ssh_ports.discard(ssh_port)
        await websocket.send_json({"status": "error", "message": "Failed to open SSH port on server"})
        await websocket.close()
        return

    session_id = uuid.uuid4().hex

    # Register tunnel
    active_tunnels[subdomain] = {
        "session_id": session_id,
        "control_ws": websocket,
        "ssh_port": ssh_port,
        "ssh_server": ssh_server,
        "pending_channels": {}
    }

    logger.info(f"Registered tunnel '{subdomain}' forwarding SSH on port {ssh_port}")

    # Send welcome / configuration details to client
    await websocket.send_json({
        "status": "success",
        "subdomain": subdomain,
        "ssh_port": ssh_port
    })

    # Wait for client disconnect or ping failures
    try:
        while True:
            # Simple keep-alive ping loop — guard: only ping while this session is still registered
            await asyncio.sleep(30)
            info = active_tunnels.get(subdomain)
            if not info or info.get("session_id") != session_id:
                # This session has been evicted; exit the loop silently
                break
            try:
                await websocket.send_json({"action": "ping"})
            except Exception:
                # WS already closed (evicted or disconnected)
                break
    except WebSocketDisconnect:
        logger.info(f"Client '{subdomain}' disconnected.")
    except Exception as e:
        logger.error(f"Error on control websocket for '{subdomain}': {e}")
    finally:
        # Cleanup allocated resources only if it belongs to this session
        info = active_tunnels.get(subdomain)
        if info and info.get("session_id") == session_id:
            active_tunnels.pop(subdomain, None)
            info["ssh_server"].close()
            await info["ssh_server"].wait_closed()
            used_ssh_ports.discard(info["ssh_port"])
            logger.info(f"Cleaned up tunnel '{subdomain}' and released port {info['ssh_port']}.")


@app.websocket("/ws/data")
async def ws_data(websocket: WebSocket, channel_id: str):
    """
    Pipes raw data to/from the local app.
    When a client receives an `open_channel` request, it connects here to hook
    up the data pipeline.
    """
    await websocket.accept()
    
    # Find the corresponding channel info
    channel_info = None
    for sub, tunnel in active_tunnels.items():
        if channel_id in tunnel["pending_channels"]:
            channel_info = tunnel["pending_channels"].pop(channel_id)
            break
    
    if not channel_info:
        logger.warning(f"Data channel connection received for unknown/expired channel_id: {channel_id}")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unknown channel_id")
        return

    tcp_reader = channel_info["tcp_reader"]
    tcp_writer = channel_info["tcp_writer"]
    initial_data = channel_info["initial_data"]
    close_fut = channel_info["close_fut"]
    connected_fut = channel_info.get("connected_fut")
    
    if connected_fut and not connected_fut.done():
        connected_fut.set_result(True)
    
    try:
        await pipe_websocket_tcp(websocket, tcp_reader, tcp_writer, initial_data)
    finally:
        if not close_fut.done():
            close_fut.set_result(True)


# --- TCP Host-Based Proxy Server (Port 8000) ---
async def start_http_routing_server(tcp_port: int):
    """
    Starts a raw TCP server that inspects the HTTP Host header of incoming connections
    and routes them to the correct client tunnel.
    """
    async def handle_http_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        try:
            # Read first chunk of data containing HTTP request headers
            initial_data = await reader.read(4096)
            if not initial_data:
                writer.close()
                return

            # Log first line of HTTP request for debugging
            request_line = initial_data.split(b"\r\n")[0].decode("utf-8", errors="ignore")
            logger.info(f"[HTTP-ROUTER] New connection from {peer}: {request_line}")

            # Find the Host header
            host_match = re.search(b'(?i)host:\\s*([a-zA-Z0-9.-]+)', initial_data)
            if not host_match:
                logger.warning(f"[HTTP-ROUTER] No Host header from {peer}")
                # No host header, reject
                writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\nNo Host Header")
                await writer.drain()
                writer.close()
                return

            host = host_match.group(1).decode("utf-8", errors="ignore")
            subdomain = get_subdomain_from_host(host)
            logger.info(f"[HTTP-ROUTER] host={host}  subdomain={subdomain}  active_tunnels={list(active_tunnels.keys())}")

            if not subdomain or subdomain not in active_tunnels:
                # Subdomain not registered or inactive
                logger.warning(f"[HTTP-ROUTER] 502 — subdomain '{subdomain}' not in active_tunnels")
                html_resp = f"""HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n
                <html>
                    <head><title>502 Tunnel Not Found</title></head>
                    <body style="font-family: sans-serif; text-align: center; padding-top: 100px;">
                        <h1 style="color: #e51c23;">502 Tunnel Offline</h1>
                        <p>The tunnel subdomain <strong>{subdomain or host}</strong> is offline or not registered.</p>
                    </body>
                </html>"""
                writer.write(html_resp.encode("utf-8"))
                await writer.drain()
                writer.close()
                return

            # Subdomain is active. Route it!
            tunnel = active_tunnels[subdomain]
            channel_id = str(uuid.uuid4())
            loop = asyncio.get_running_loop()
            close_fut = loop.create_future()
            connected_fut = loop.create_future()

            tunnel["pending_channels"][channel_id] = {
                "tcp_reader": reader,
                "tcp_writer": writer,
                "initial_data": initial_data,
                "close_fut": close_fut,
                "connected_fut": connected_fut
            }

            logger.info(f"[HTTP-ROUTER] Channel {channel_id[:8]} created for '{subdomain}' — notifying client")

            # Notify the client to open an HTTP tunnel channel
            # Guard: the control_ws may have just been closed (eviction race)
            try:
                await tunnel["control_ws"].send_json({
                    "action": "open_channel",
                    "channel_id": channel_id,
                    "type": "http"
                })
            except Exception as send_err:
                # Control WS closed before we could notify — clean up and bail
                tunnel["pending_channels"].pop(channel_id, None)
                if not close_fut.done():
                    close_fut.cancel()
                logger.warning(f"[HTTP-ROUTER] control_ws send FAILED for '{subdomain}' ch={channel_id[:8]}: {send_err}")
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\nTunnel disconnected")
                try:
                    await writer.drain()
                except Exception:
                    pass
                writer.close()
                return

            # Wait for data ws connection (handshake) within 60 seconds
            logger.info(f"[HTTP-ROUTER] Waiting for client WS handshake ch={channel_id[:8]}")
            try:
                await asyncio.wait_for(connected_fut, timeout=60.0)
                logger.info(f"[HTTP-ROUTER] Handshake OK ch={channel_id[:8]} — piping data")
            except asyncio.TimeoutError:
                logger.warning(f"[HTTP-ROUTER] Handshake TIMEOUT (60s) for ch={channel_id[:8]}")
                tunnel["pending_channels"].pop(channel_id, None)
                writer.close()
                return

            # Wait indefinitely for completion of piping
            try:
                await close_fut
                logger.info(f"[HTTP-ROUTER] Pipe complete ch={channel_id[:8]}")
            except (asyncio.CancelledError, Exception) as e:
                logger.debug(f"[HTTP-ROUTER] Pipe ended ch={channel_id[:8]}: {e}")

        except Exception as e:
            logger.error(f"[HTTP-ROUTER] Unhandled error from {peer}: {e}")
            writer.close()

    server = await asyncio.start_server(handle_http_conn, "0.0.0.0", tcp_port)
    logger.info(f"HTTP Routing TCP Server listening on 0.0.0.0:{tcp_port}")
    return server


async def run_server(host: str, api_port: int, tcp_port: int, token: str, domain: str = "s.yourdomain.com"):
    """Runs the main server combining FastAPI and the TCP Host Router."""
    global TOKEN, DOMAIN
    TOKEN = token
    DOMAIN = domain
    
    # Start dynamic HTTP router
    http_router = await start_http_routing_server(tcp_port)
    
    # Start FastAPI API & WS server
    config = uvicorn.Config(app, host=host, port=api_port, log_level="info", ws_ping_interval=None)
    server = uvicorn.Server(config)
    
    try:
        await server.serve()
    finally:
        http_router.close()
        await http_router.wait_closed()
        logger.info("Server stopped.")
