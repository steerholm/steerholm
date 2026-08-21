import typer
import asyncio
from pathlib import Path
from typing import List, Optional
from rich.console import Console
from rich.table import Table
from . import __version__
from .config import ConfigManager
from .updater import UpdateError, run_update_installer, update_binary

app = typer.Typer(help="MCP Harbour: Manage your MCP servers and permissions.")
console = Console()
err_console = Console(stderr=True)
config_manager = ConfigManager()

# How often the "update available" check refreshes over the network.
_UPDATE_CHECK_INTERVAL = 86400  # 24h


@app.callback()
def _root(ctx: typer.Context):
    """Register a best-effort 'update available' hint after interactive commands."""
    if ctx.resilient_parsing:  # shell completion
        return
    if ctx.invoked_subcommand in (None, "update", "version", "serve"):
        return
    ctx.call_on_close(_maybe_notify_update)


def _update_cache_path():
    from . import config
    return config.CONFIG_DIR / "update-check.json"


def _maybe_notify_update() -> None:
    """Print a hint if a newer release is available. The network check is throttled
    to once per interval with a short timeout; the rest of the time it reads a small
    cache. Best-effort — it never blocks meaningfully or fails the command."""
    import json
    import os
    import time
    from .updater import fetch_latest_tag, is_newer

    if os.environ.get("MCP_HARBOUR_NO_UPDATE_CHECK"):
        return

    path = _update_cache_path()
    try:
        cache = json.loads(path.read_text())
    except Exception:
        cache = {}

    now = time.time()
    latest = cache.get("latest")
    if now - cache.get("checked_at", 0) > _UPDATE_CHECK_INTERVAL:
        # Refresh once per interval; on failure, back off for the full interval.
        try:
            latest = fetch_latest_tag(timeout=2.0).lstrip("v")
        except Exception:
            latest = cache.get("latest")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"checked_at": now, "latest": latest}))
            tmp.replace(path)
        except Exception:
            pass

    if latest and is_newer(latest, __version__):
        err_console.print(
            f"[yellow]A new release of Harbour is available:[/yellow] "
            f"{__version__} -> {latest}. Run [bold]harbour update[/bold]."
        )

# Sub-typer for identity management
identity_app = typer.Typer()
app.add_typer(identity_app, name="identity", help="Manage identities (Captains)")

# Sub-typer for permission management
permit_app = typer.Typer()
app.add_typer(permit_app, name="permit", help="Manage permissions (Policies)")


def _handle(fn, *args, **kwargs):
    """Call a service method and display any error cleanly."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)


@app.command()
def version():
    """Show the installed Harbour version."""
    console.print(__version__)


@app.command()
def update(
    tag: Optional[str] = typer.Option(None, help="Release tag to install (default: latest)"),
    check: bool = typer.Option(False, "--check", help="Check for updates without installing"),
    force: bool = typer.Option(False, "--force", help="Reinstall the selected version even if it is not newer"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Install without confirmation"),
):
    """Update Harbour from a GitHub release (latest by default)."""
    import logging
    # Surface updater warnings (e.g. skipped checksum verification) on stderr.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        info = update_binary(tag=tag, check_only=True, force=force)
    except UpdateError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    if check:
        if info.update_available:
            console.print(f"[green]Update available:[/green] {__version__} -> {info.tag}")
        else:
            console.print(f"[green]Harbour is up to date:[/green] {__version__}")
        return

    # An explicit --tag is a request to install that exact version (a downgrade or
    # reinstall), so only short-circuit "up to date" when no tag was named.
    if not info.update_available and not force and tag is None:
        console.print(f"[green]Harbour is already up to date:[/green] {__version__}")
        return

    if not yes:
        typer.confirm(f"Install Harbour {info.tag}?", abort=True)

    try:
        run_update_installer(info.tag)
    except UpdateError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    console.print(f"[bold green]Updated Harbour to {info.tag}.[/bold green]")


@app.command()
def dock(
    name: str = typer.Option(..., help="Name of the server/ship"),
    command: Optional[str] = typer.Option(None, help="Full command to run the server (stdio)"),
    url: Optional[str] = typer.Option(None, help="Server URL (streamable HTTP)"),
):
    """
    Dock (install/register) a new MCP server.

    Provide --command for stdio servers or --url for HTTP servers (not both).

    Examples:
      harbour dock --name filesystem --command "npx -y @modelcontextprotocol/server-filesystem /home/user"
      harbour dock --name remote-api --url "http://localhost:8000/mcp"
    """
    _handle(config_manager.add_server, name, command=command, url=url)
    console.print(f"[bold green]Success:[/bold green] Server '{name}' docked successfully!")
    _notify_daemon_reconcile()


@app.command()
def undock(name: str):
    """Undock (remove) an MCP server."""
    _handle(config_manager.remove_server, name)
    console.print(f"[bold green]Success:[/bold green] Server '{name}' undocked.")
    _notify_daemon_reconcile()


def _notify_daemon_reconcile() -> None:
    """Tell a running daemon to apply docked-server changes now (the CLI drives
    the daemon, like `docker` drives `dockerd`). No-op with a note if it's down —
    the change is persisted in config and applied when the daemon next starts.
    """
    import json
    import urllib.request
    from .config import DEFAULT_HOST, DEFAULT_PORT, get_or_create_control_token

    if not _harbour_up(DEFAULT_HOST, DEFAULT_PORT):
        console.print("[yellow]Daemon is not running; the change applies when it starts.[/yellow]")
        return
    try:
        token = get_or_create_control_token()
        req = urllib.request.Request(
            f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/control/reconcile",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read() or b"{}")
    except Exception as e:
        console.print(f"[yellow]Could not reach the daemon to apply the change: {e}[/yellow]")
        return

    failed = result.get("failed") or []
    started = result.get("started") or []
    stopped = result.get("stopped") or []
    if failed:
        console.print(f"[bold red]Daemon could not start:[/bold red] {', '.join(failed)} (check the daemon log)")
    if started:
        console.print(f"[green]Daemon started:[/green] {', '.join(started)}")
    if stopped:
        console.print(f"[green]Daemon stopped:[/green] {', '.join(stopped)}")


def _daemon_server_status() -> Optional[dict]:
    """Fetch live per-server status from the running daemon, or None if it's down."""
    import json
    import urllib.request
    from .config import DEFAULT_HOST, DEFAULT_PORT, get_or_create_control_token

    if not _harbour_up(DEFAULT_HOST, DEFAULT_PORT):
        return None
    try:
        token = get_or_create_control_token()
        req = urllib.request.Request(
            f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/control/servers",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read() or b"{}")
    except Exception:
        return None


def _format_uptime(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def _status_markup(state: str) -> str:
    # ASCII-only: the output is captured/piped and decoded on other platforms
    # (Windows cp1252), so avoid glyphs that don't round-trip through every locale.
    return {
        "running": "[green]running[/green]",
        "failed": "[red]failed[/red]",
        "stopped": "[yellow]stopped[/yellow]",
    }.get(state, "[dim]unknown[/dim]")


@app.command("list")
def list_servers():
    """List all docked MCP servers with their live status."""
    servers = config_manager.list_servers()
    if not servers:
        console.print("No servers docked.")
        return

    status = _daemon_server_status()

    table = Table(title="Docked Ships (MCP Servers)")
    table.add_column("Name", style="cyan")
    table.add_column("Command", style="magenta")
    table.add_column("Type", style="green")
    table.add_column("Status")
    table.add_column("Uptime", style="dim")
    table.add_column("Tools", justify="right")
    for server in servers:
        st = (status or {}).get(server.name, {})
        state = st.get("state", "unknown" if status is None else "stopped")
        n_tools = len(st.get("tools", [])) if st else 0
        table.add_row(
            server.name,
            server.command or server.url,
            server.server_type.value,
            _status_markup(state),
            _format_uptime(st.get("uptime_seconds")),
            str(n_tools) if state == "running" else "-",
        )
    console.print(table)
    if status is None:
        console.print("[dim]Daemon not running; live status unavailable.[/dim]")


@app.command()
def inspect(name: str):
    """Inspect a docked server: its config, live status, and the tools it provides."""
    server = config_manager.get_server(name)
    if not server:
        console.print(f"[bold red]Error:[/bold red] Server '{name}' not found.")
        raise typer.Exit(code=1)

    console.print(f"[bold]Name:[/bold] {server.name}")
    if server.command:
        console.print(f"[bold]Command:[/bold] {server.command}")
    if server.url:
        console.print(f"[bold]URL:[/bold] {server.url}")
    console.print(f"[bold]Env:[/bold] {server.env}")
    console.print(f"[bold]Type:[/bold] {server.server_type.value}")

    status = _daemon_server_status()
    if status is None:
        console.print("[bold]Status:[/bold] [dim]daemon not running[/dim]")
        return

    st = status.get(name)
    if st is None:
        console.print("[bold]Status:[/bold] [dim]unknown[/dim]")
        return

    console.print(f"[bold]Status:[/bold] {_status_markup(st['state'])}")
    if st.get("uptime_seconds") is not None:
        console.print(f"[bold]Uptime:[/bold] {_format_uptime(st['uptime_seconds'])}")
    if st.get("error"):
        console.print(f"[bold red]Error:[/bold red] {st['error']}")

    tools = st.get("tools") or []
    if tools:
        tools_table = Table(title=f"Tools ({len(tools)})")
        tools_table.add_column("Tool", style="cyan")
        tools_table.add_column("Description", style="white")
        for tool in tools:
            desc = (tool.get("description") or "").strip().split("\n")[0]
            tools_table.add_row(tool["name"], desc)
        console.print(tools_table)
    elif st["state"] == "running":
        console.print("[dim]This server exposes no tools.[/dim]")


@app.command()
def serve(
    host: str = typer.Option(None, help="Host to bind (default: 127.0.0.1)"),
    port: int = typer.Option(None, help="Port to bind (default: 4767)"),
):
    """Start the Harbour Daemon in the foreground."""
    from .gateway import HarbourGateway
    from .config import DEFAULT_HOST, DEFAULT_PORT
    import sys
    import logging

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    serve_host = host or DEFAULT_HOST
    serve_port = port or DEFAULT_PORT

    gateway = HarbourGateway()
    sys.stderr.write(f"Starting Harbour Daemon (http://{serve_host}:{serve_port}/mcp)...\n")
    asyncio.run(gateway.serve(serve_host, serve_port))


# Windows runs the daemon as a per-user logon Scheduled Task (the mirror of the
# systemd --user unit on Linux and the LaunchAgent on macOS): it runs as the user
# in their session, with no admin and no stored password.
WIN_TASK_NAME = "MCPHarbour"


def _harbour_up(host: str, port: int, timeout: float = 1.0) -> bool:
    """True if a Harbour daemon (not just any listener) answers on host:port.

    Probes the unauthenticated /healthz endpoint and checks the service
    signature, so a different process holding the port is not a false positive.
    """
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=timeout) as resp:
            data = json.loads(resp.read() or b"{}")
        return data.get("service") == "mcp-harbour"
    except Exception:
        return False


@app.command()
def start():
    """Start the Harbour Daemon via the platform service manager."""
    import subprocess
    import sys

    if sys.platform == "linux":
        subprocess.run(["systemctl", "--user", "start", "mcp-harbour"], check=True)
    elif sys.platform == "darwin":
        plist = f"{Path.home()}/Library/LaunchAgents/dev.mcp-harbour.daemon.plist"
        subprocess.run(["launchctl", "load", plist], check=True)
    elif sys.platform == "win32":
        import time
        from .config import DEFAULT_HOST, DEFAULT_PORT
        result = subprocess.run(
            ["schtasks", "/Run", "/TN", WIN_TASK_NAME],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            console.print(f"[bold red]Error:[/bold red] {result.stderr.strip() or 'Failed to start daemon.'}")
            raise typer.Exit(1)
        # /Run only triggers the task; confirm the daemon actually came up.
        for _ in range(20):
            if _harbour_up(DEFAULT_HOST, DEFAULT_PORT):
                break
            time.sleep(0.5)
        else:
            console.print(
                f"[yellow]Triggered the task, but nothing is listening on {DEFAULT_HOST}:{DEFAULT_PORT} yet. "
                "On a headless session the daemon starts at your next logon.[/yellow]"
            )
            raise typer.Exit(1)
    else:
        console.print("[bold red]Unsupported platform.[/bold red]")
        raise typer.Exit(1)
    console.print("[bold green]Daemon started.[/bold green]")


@app.command()
def stop():
    """Stop the Harbour Daemon via the platform service manager."""
    import subprocess
    import sys

    if sys.platform == "linux":
        subprocess.run(["systemctl", "--user", "stop", "mcp-harbour"], check=True)
    elif sys.platform == "darwin":
        plist = f"{Path.home()}/Library/LaunchAgents/dev.mcp-harbour.daemon.plist"
        subprocess.run(["launchctl", "unload", plist], check=True)
    elif sys.platform == "win32":
        import time
        from .config import DEFAULT_HOST, DEFAULT_PORT
        # /End fails harmlessly when nothing is running; the port is the source of
        # truth, so an already-stopped daemon is reported as stopped (exit 0).
        subprocess.run(
            ["schtasks", "/End", "/TN", WIN_TASK_NAME],
            capture_output=True, text=True,
        )
        for _ in range(10):
            if not _harbour_up(DEFAULT_HOST, DEFAULT_PORT):
                break
            time.sleep(0.5)
        else:
            console.print(
                f"[bold red]Error:[/bold red] Daemon is still listening on {DEFAULT_HOST}:{DEFAULT_PORT}."
            )
            raise typer.Exit(1)
    else:
        console.print("[bold red]Unsupported platform.[/bold red]")
        raise typer.Exit(1)
    console.print("[bold green]Daemon stopped.[/bold green]")


@app.command()
def status():
    """Check if the Harbour Daemon is running."""
    import subprocess
    import sys

    if sys.platform == "linux":
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "mcp-harbour"],
            capture_output=True, text=True
        )
        state = result.stdout.strip()
        if state == "active":
            console.print("[bold green]Daemon is running.[/bold green]")
        else:
            console.print(f"[yellow]Daemon is {state}.[/yellow]")
    elif sys.platform == "darwin":
        result = subprocess.run(
            ["launchctl", "list", "dev.mcp-harbour.daemon"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            console.print("[bold green]Daemon is running.[/bold green]")
        else:
            console.print("[yellow]Daemon is not running.[/yellow]")
    elif sys.platform == "win32":
        # Check the daemon directly (locale-proof, unlike parsing schtasks text).
        from .config import DEFAULT_HOST, DEFAULT_PORT
        if _harbour_up(DEFAULT_HOST, DEFAULT_PORT):
            console.print("[bold green]Daemon is running.[/bold green]")
        else:
            console.print("[yellow]Daemon is not running.[/yellow]")
    else:
        console.print("[bold red]Unsupported platform.[/bold red]")
        raise typer.Exit(1)


@identity_app.command("create")
def identity_create(name: str):
    """Create a new identity (Captain) and generate an API key."""
    api_key = _handle(config_manager.add_identity, name)
    console.print(f"[bold green]Identity '{name}' created successfully![/bold green]")
    console.print(f"API Key: [bold]{api_key}[/bold]")
    console.print("[yellow]Keep this key safe! It won't be shown again.[/yellow]")


@identity_app.command("list")
def identity_list():
    """List all identities."""
    identities = config_manager.config.identities
    if not identities:
        console.print("No identities found.")
        return

    table = Table(title="Docked Captains (Identities)")
    table.add_column("Name", style="cyan")
    table.add_column("API Key Prefix", style="magenta")
    for name, identity in identities.items():
        table.add_row(name, identity.key_prefix)
    console.print(table)


@identity_app.command("delete")
def identity_delete(name: str):
    """Delete an identity (Captain) and its policy."""
    _handle(config_manager.remove_identity, name)
    console.print(f"[bold green]Success:[/bold green] Identity '{name}' deleted.")


@permit_app.command("allow")
def permit_allow(
    identity: str,
    server: str,
    tool: str = typer.Option("*", help="Tool name or glob pattern (default: *)"),
    args: Optional[List[str]] = typer.Option(
        None, help="Argument policies: 'arg=pattern' (glob) or 'arg=re:pattern' (regex)"
    ),
):
    """
    Grant permission to an identity.

    Examples:
      harbour permit allow agent filesystem
      harbour permit allow agent filesystem --tool "read_*" --args "path=/home/user/**"
      harbour permit allow agent db --tool "query" --args "sql=re:^SELECT.*" "db=production"
    """
    if not config_manager.get_server(server) and server != "*":
        console.print(f"[yellow]Warning: Server '{server}' is not currently docked.[/yellow]")

    _handle(config_manager.grant_permission, identity, server, tool=tool, arg_policies=args)
    console.print(f"[bold green]Permission granted for '{identity}' on '{server}' tool '{tool}'[/bold green]")


@permit_app.command("show")
def permit_show(identity: str):
    """Show the policy for an identity."""
    policy = config_manager.load_policy(identity)
    if not policy:
        console.print(f"[yellow]No policy found for '{identity}'. (Access Denied All)[/yellow]")
        return

    console.print(f"[bold]Policy for {identity}:[/bold]")
    for server, tools in policy.permissions.items():
        console.print(f"  Server: [cyan]{server}[/cyan]")
        for tool in tools:
            pol_str = ""
            if tool.policies:
                pol_str = " -> " + ", ".join(
                    f"{p.arg_name}={'re:' if p.match_type == 'regex' else ''}{p.pattern}"
                    for p in tool.policies
                )
            console.print(f"    - Tool: [green]{tool.name}[/green]{pol_str}")


if __name__ == "__main__":  # pragma: no cover
    app()
