import asyncio
import os
import sys
import tempfile
import subprocess
import shutil
import logging
import urllib.parse
from typing import Optional
import websockets
from rich.console import Console

console = Console()
logger = logging.getLogger("raytunnel.client")

def setup_ssh_daemon() -> int:
    """
    Sets up and starts a local SSH daemon (sshd) in user-space if not already running.
    Returns the port the SSH daemon is listening on.
    """
    # 1. Check if sshd is installed
    sshd_path = shutil.which("sshd") or "/usr/sbin/sshd"
    if not os.path.exists(sshd_path):
        console.print("[yellow]WARNING: sshd not found. Attempting to install openssh-server...[/yellow]")
        try:
            subprocess.run(["apt-get", "update", "-qq"], check=True)
            subprocess.run(["apt-get", "install", "-y", "-qq", "openssh-server"], check=True)
            sshd_path = shutil.which("sshd") or "/usr/sbin/sshd"
        except Exception as e:
            console.print(f"[red]Failed to install openssh-server: {e}. SSH function might not work.[/red]")
            return -1

    # 2. Check if we have key pair and add to authorized_keys
    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    authorized_keys_path = os.path.join(ssh_dir, "authorized_keys")

    # Generate a temporary keypair for raytunnel if it doesn't exist
    key_path = os.path.expanduser("~/.ssh/raytunnel_id_ed25519")
    if not os.path.exists(key_path):
        console.print("[cyan]Generating temporary SSH keypair for client terminal access...[/cyan]")
        try:
            subprocess.run([
                "ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", "", "-q"
            ], check=True)
            # Ensure correct permissions
            os.chmod(key_path, 0o600)
        except Exception as e:
            console.print(f"[red]Failed to generate SSH keypair: {e}[/red]")
            return -1

    # Read the public key
    pub_key_path = key_path + ".pub"
    with open(pub_key_path, "r") as f:
        pub_key = f.read().strip()

    # Add public key to authorized_keys if not present
    existing_keys = ""
    if os.path.exists(authorized_keys_path):
        with open(authorized_keys_path, "r") as f:
            existing_keys = f.read()

    if pub_key not in existing_keys:
        with open(authorized_keys_path, "a") as f:
            f.write(f"\n{pub_key}\n")
        os.chmod(authorized_keys_path, 0o600)

    # 3. Create host keys and user-space sshd config
    temp_dir = tempfile.gettempdir()
    host_key_path = os.path.join(temp_dir, "raytunnel_ssh_host_ed25519_key")
    if not os.path.exists(host_key_path):
        try:
            subprocess.run([
                "ssh-keygen", "-t", "ed25519", "-f", host_key_path, "-N", "", "-q"
            ], check=True)
            os.chmod(host_key_path, 0o600)
        except Exception as e:
            console.print(f"[red]Failed to generate host key: {e}[/red]")
            return -1

    # Use a custom port (e.g. 2222) to avoid requiring root privileges
    sshd_port = 2222
    sshd_config_content = f"""
Port {sshd_port}
HostKey {host_key_path}
AuthorizedKeysFile {authorized_keys_path}
ChallengeResponseAuthentication no
PasswordAuthentication no
UsePAM no
PidFile {os.path.join(temp_dir, "raytunnel_sshd.pid")}
Subsystem sftp /usr/lib/openssh/sftp-server
StrictModes no
"""
    sshd_config_path = os.path.join(temp_dir, "raytunnel_sshd_config")
    with open(sshd_config_path, "w") as f:
        f.write(sshd_config_content)

    # 4. Start the sshd daemon
    try:
        # sshd requires /run/sshd to exist on many systems, otherwise it fails to start
        os.makedirs("/run/sshd", exist_ok=True)
    except Exception as e:
        console.print(f"[yellow]Warning: could not create /run/sshd: {e}[/yellow]")

    try:
        # Check if already running by trying to read PID
        pid_file = os.path.join(temp_dir, "raytunnel_sshd.pid")
        if os.path.exists(pid_file):
            with open(pid_file, "r") as f:
                pid = int(f.read().strip())
            # Check if process actually exists
            try:
                os.kill(pid, 0)
                console.print(f"[green]SSH daemon already running on port {sshd_port} (PID {pid})[/green]")
                return sshd_port
            except OSError:
                # PID is stale, remove pid file
                os.remove(pid_file)
    except Exception:
        pass

    try:
        # Start sshd process and redirect output to a log file for diagnostics
        sshd_log_path = os.path.join(temp_dir, "raytunnel_sshd.log")
        log_file = open(sshd_log_path, "w")
        proc = subprocess.Popen(
            [sshd_path, "-f", sshd_config_path, "-D"], 
            stdout=log_file, 
            stderr=log_file
        )
        
        # Give it a short moment to check if it crashed on startup
        import time
        time.sleep(0.8)
        if proc.poll() is not None:
            log_file.close()
            try:
                with open(sshd_log_path, "r") as lf:
                    log_content = lf.read().strip()
            except Exception:
                log_content = ""
            console.print(f"[red]SSH daemon exited immediately with code {proc.poll()}[/red]")
            if log_content:
                console.print(f"[red]sshd error log:\n{log_content}[/red]")
            return -1
            
        console.print(f"[green]SSH daemon started successfully on port {sshd_port}[/green]")
        return sshd_port
    except Exception as e:
        console.print(f"[red]Failed to start SSH daemon: {e}[/red]")
        return -1


async def pipe_streams(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ws: websockets.WebSocketClientProtocol):
    """Pipes data bidirectionally between a local TCP connection and a remote WebSocket."""
    async def tcp_to_ws():
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                await ws.send(data)
        except Exception as e:
            logger.debug(f"TCP to WS error: {e}")

    async def ws_to_tcp():
        try:
            async for message in ws:
                if isinstance(message, str):
                    message = message.encode('utf-8')
                writer.write(message)
                await writer.drain()
        except Exception as e:
            logger.debug(f"WS to TCP error: {e}")

    t1 = asyncio.create_task(tcp_to_ws())
    t2 = asyncio.create_task(ws_to_tcp())
    try:
        done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        if t1 not in done:
            # t2 (remote→local) finished first (e.g. finished receiving request body).
            # Wait for t1 (local→remote) so the response can flow back fully.
            await t1
    finally:
        t1.cancel()
        t2.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


async def handle_channel(server_url: str, channel_id: str, local_port: int, ssl: bool):
    """Handles an individual tunnel channel by forwarding to the local TCP port."""
    ws_scheme = "wss" if ssl else "ws"
    parsed = urllib.parse.urlparse(server_url)
    # The actual network location of the server (e.g. s.ray.ac:8001 or s.ray.ac)
    netloc = parsed.netloc or parsed.path
    if ":" not in netloc and not ssl:
        # Default ws port if not specified
        netloc = f"{netloc}:8001"

    data_url = f"{ws_scheme}://{netloc}/ws/data?channel_id={channel_id}"
    
    reader, writer = None, None
    ws = None
    try:
        # Connect to local port
        reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
        # Connect to server's data WebSocket
        ws = await websockets.connect(data_url, open_timeout=30, ping_interval=None)
        # Pipe them
        await pipe_streams(reader, writer, ws)
    except Exception as e:
        logger.error(f"Error handling channel {channel_id} (port {local_port}): {e}")
        if writer:
            writer.close()
        if ws:
            await ws.close()


async def run_client(
    server: str,
    token: str,
    http_port: int,
    ssh: bool = True,
    subdomain: Optional[str] = None,
    ssl: bool = True
):
    """
    Main client loop. Connects to server control channel and listens for incoming connections.
    """
    ws_scheme = "wss" if ssl else "ws"
    # Ensure server doesn't contain scheme
    server_clean = server
    if "://" in server:
        server_clean = server.split("://")[1]

    # Connect to the backend control port (typically 8001 for WS API, unless specified)
    control_netloc = server_clean
    if ":" not in control_netloc and not ssl:
        control_netloc = f"{control_netloc}:8001"

    params = {"token": token}
    if subdomain:
        params["subdomain"] = subdomain
    
    query_str = urllib.parse.urlencode(params)
    control_url = f"{ws_scheme}://{control_netloc}/ws/control?{query_str}"

    # Setup SSH daemon if requested
    ssh_local_port = -1
    if ssh:
        ssh_local_port = setup_ssh_daemon()

    console.print(f"[cyan]Connecting to tunnel server at {control_url}...[/cyan]")
    
    while True:
        try:
            async with websockets.connect(control_url, open_timeout=30) as ws:
                # Expect welcome message
                import json
                welcome_msg = await ws.recv()
                config = json.loads(welcome_msg)
                
                if config.get("status") == "error":
                    console.print(f"[red]Server error: {config.get('message')}[/red]")
                    return

                allocated_subdomain = config.get("subdomain")
                allocated_ssh_port = config.get("ssh_port")
                server_host = server_clean.split(":")[0]
                public_http_url = f"https://{allocated_subdomain}.{server_host}" if ssl else f"http://{allocated_subdomain}.{server_host}"
                
                console.print("\n" + "="*60)
                console.print(f"[green]✔ Raytunnel Established Successfully![/green]")
                console.print(f"  [bold]HTTP Web App URL:[/bold] [underline]{public_http_url}[/underline]")
                if ssh and allocated_ssh_port:
                    key_path = os.path.expanduser("~/.ssh/raytunnel_id_ed25519")
                    try:
                        with open(key_path, "r") as kf:
                            private_key_content = kf.read().strip()
                    except Exception as e:
                        private_key_content = f"Error reading key: {e}"
                    # console.print(f"  [bold]SSH Command:[/bold]      ssh -i ~/.ssh/raytunnel_id_ed25519 -p {allocated_ssh_port} root@{server_clean.split(':')[0]}")
                    # console.print(f"  [bold]SSH Port:[/bold]         {allocated_ssh_port}")
                    # console.print(f"  [bold]SSH Private Key (Copy & save to ~/.ssh/raytunnel_id_ed25519 on your PC):[/bold]")
                    # console.print(f"\n{private_key_content}\n")
                else:
                    private_key_content = ""
                import raytunnel
                raytunnel.allocated_info = {
                    "subdomain": allocated_subdomain,
                    "ssh_port": allocated_ssh_port,
                    "server": server_clean.split(":")[0],
                    "key": private_key_content
                }
                raytunnel.allocated_event.set()
                console.print("="*60 + "\n")

                # Listen for control messages
                async for message in ws:
                    event = json.loads(message)
                    action = event.get("action")
                    if action == "open_channel":
                        channel_id = event.get("channel_id")
                        channel_type = event.get("type")
                        
                        target_port = http_port if channel_type == "http" else ssh_local_port
                        if target_port <= 0:
                            logger.error(f"Cannot open channel for type '{channel_type}' as port is invalid.")
                            continue

                        # Spawn task to handle the connection
                        asyncio.create_task(
                            handle_channel(
                                server_url=control_url,
                                channel_id=channel_id,
                                local_port=target_port,
                                ssl=ssl
                            )
                        )
        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            console.print(f"[yellow]Connection lost ({e}). Reconnecting in 5 seconds...[/yellow]")
            await asyncio.sleep(5)
        except Exception as e:
            console.print(f"[red]Unexpected client error: {e}[/red]")
            await asyncio.sleep(5)
