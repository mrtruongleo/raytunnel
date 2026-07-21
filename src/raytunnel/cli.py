import asyncio
import sys
import click
import logging
from raytunnel.client import run_client
from raytunnel.server import run_server

# Set up logging level based on input
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

@click.group()
def main():
    """Raytunnel: A lightweight reverse tunnel and SSH forwarding tool."""
    pass

@main.command()
@click.option("--server", "-s", required=True, help="Domain/IP of the raytunnel server (e.g. s.yourdomain.com)")
@click.option("--token", "-t", required=True, envvar="RAYTUNNEL_TOKEN", help="Authentication token for the server")
@click.option("--port", "-p", default=8000, type=int, help="Local HTTP port to forward (e.g. your web app port)")
@click.option("--ssh/--no-ssh", default=True, help="Enable SSH forward terminal (launches user-space sshd)")
@click.option("--subdomain", "-d", default=None, help="Desired custom subdomain prefix (e.g. 'my-app')")
@click.option("--ssl/--no-ssl", default=True, help="Use WebSocket Secure (wss://) connection")
def client(server, token, port, ssh, subdomain, ssl):
    """Start the tunnel client forwarding local HTTP and SSH to the server."""
    try:
        asyncio.run(
            run_client(
                server=server,
                token=token,
                http_port=port,
                ssh=ssh,
                subdomain=subdomain,
                ssl=ssl
            )
        )
    except KeyboardInterrupt:
        click.echo("\nTunnel stopped by user.")
        sys.exit(0)
    except Exception as e:
        click.echo(f"Client error: {e}", err=True)
        sys.exit(1)

@main.command()
@click.option("--host", default="0.0.0.0", help="API and WebSockets bind address")
@click.option("--port", default=8001, type=int, help="API and WebSockets bind port (control port)")
@click.option("--tcp-port", default=8000, type=int, help="TCP HTTP Host-routing entrypoint port")
@click.option("--token", "-t", required=True, envvar="RAYTUNNEL_TOKEN", help="Token clients must provide to connect")
@click.option("--domain", "-d", default="s.yourdomain.com", help="Main domain of the server for routing")
def server(host, port, tcp_port, token, domain):
    """Start the raytunnel server."""
    try:
        asyncio.run(
            run_server(
                host=host,
                api_port=port,
                tcp_port=tcp_port,
                token=token,
                domain=domain
            )
        )
    except KeyboardInterrupt:
        click.echo("\nServer stopped by user.")
        sys.exit(0)
    except Exception as e:
        click.echo(f"Server error: {e}", err=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
