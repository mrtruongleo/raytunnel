import asyncio
import threading
from typing import Optional
from raytunnel.client import run_client

__version__ = "0.1.0"
allocated_info = {}
# Event that is set once allocated_info has been populated by the client.
# Call allocated_event.wait(timeout=N) in the caller to block until the
# tunnel is established rather than busy-polling.
allocated_event = threading.Event()

def _run_async_loop(loop, server, token, http_port, ssh, subdomain, ssl):
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            run_client(
                server=server,
                token=token,
                http_port=http_port,
                ssh=ssh,
                subdomain=subdomain,
                ssl=ssl
            )
        )
    except Exception as e:
        print(f"Raytunnel background worker encountered an error: {e}")


class TunnelHandle:
    """Handle to manage a background tunnel worker."""
    def __init__(self, thread: threading.Thread, loop: asyncio.AbstractEventLoop):
        self.thread = thread
        self.loop = loop

    def stop(self):
        """Stops the background tunnel."""
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        print("Raytunnel background worker stopped.")


def start(
    server: str,
    token: str,
    http_port: int,
    ssh: bool = True,
    subdomain: Optional[str] = None,
    ssl: bool = True,
    background: bool = False
) -> Optional[TunnelHandle]:
    """
    Starts the raytunnel client.
    
    If background=True, starts it in a separate thread and returns a TunnelHandle
    so it doesn't block the caller (ideal for notebooks).
    Otherwise, blocks the caller and runs in the main thread.
    """
    if background:
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=_run_async_loop,
            args=(loop, server, token, http_port, ssh, subdomain, ssl),
            daemon=True
        )
        thread.start()
        return TunnelHandle(thread, loop)
    else:
        asyncio.run(
            run_client(
                server=server,
                token=token,
                http_port=http_port,
                ssh=ssh,
                subdomain=subdomain,
                ssl=ssl
            )
        )
        return None
