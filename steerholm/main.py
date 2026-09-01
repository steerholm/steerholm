import typer
import asyncio
from pathlib import Path
from typing import List, Optional
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from . import __version__
from .config import ConfigManager
from .updater import UpdateError, run_update_installer, update_binary

app = typer.Typer(
    help="Steerholm — the control plane for your MCP servers and agents.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)
config_manager = ConfigManager()

# How often the "update available" check refreshes over the network.
_UPDATE_CHECK_INTERVAL = 86400  # 24h


def _version_callback(value: bool):
    if value:
        console.print(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True,
        help="Show the installed Steerholm version and exit.",
    ),
):
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

    if os.environ.get("STEERHOLM_NO_UPDATE_CHECK"):
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
            f"[yellow]A new release of Steerholm is available:[/yellow] "
            f"{__version__} -> {latest}. Run [bold]holm update[/bold]."
        )


# Verb-first sub-typers: `holm <verb> <resource>`.
add_app = typer.Typer(no_args_is_help=True, help="Add an agent or server to Steerholm.")
app.add_typer(add_app, name="add")

remove_app = typer.Typer(no_args_is_help=True, help="Remove an agent or server.")
app.add_typer(remove_app, name="remove")

list_app = typer.Typer(no_args_is_help=True, help="List agents or servers.")
app.add_typer(list_app, name="list")

show_app = typer.Typer(no_args_is_help=True, help="Show details for an agent or server.")
app.add_typer(show_app, name="show")

rotate_app = typer.Typer(no_args_is_help=True, help="Rotate an agent's access key.")
app.add_typer(rotate_app, name="rotate")


def _handle(fn, *args, **kwargs):
    """Call a service method and display any error cleanly."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {escape(str(e))}")
        raise typer.Exit(code=1)


def _parse_env(pairs: Optional[List[str]]) -> dict:
    """Parse repeated 'KEY=VALUE' options into a dict; values may contain '='.

    Keys are trimmed of surrounding whitespace (a whitespace-padded name is an
    invalid env var the child would silently drop). A repeated key is rejected
    rather than silently overwriting an earlier value.
    """
    result: dict = {}
    for item in pairs or []:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or not key:
            console.print(f"[bold red]Error:[/bold red] --env expects KEY=VALUE, got {escape(repr(item))}")
            raise typer.Exit(code=1)
        if key in result:
            console.print(f"[bold red]Error:[/bold red] --env got {escape(repr(key))} more than once")
            raise typer.Exit(code=1)
        result[key] = value
    return result


@app.command()
def version():
    """Show the installed Steerholm version."""
    console.print(__version__)


@app.command()
def update(
    tag: Optional[str] = typer.Option(None, help="Release tag to install (default: latest)"),
    check: bool = typer.Option(False, "--check", help="Check for updates without installing"),
    force: bool = typer.Option(False, "--force", help="Reinstall the selected version even if it is not newer"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Install without confirmation"),
):
    """Update Steerholm from a GitHub release (latest by default)."""
    import logging
    # Surface updater warnings (e.g. skipped checksum verification) on stderr.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        info = update_binary(tag=tag, check_only=True, force=force)
    except UpdateError as e:
        console.print(f"[bold red]Error:[/bold red] {escape(str(e))}")
        raise typer.Exit(code=1)

    if check:
        if info.update_available:
            console.print(f"[green]Update available:[/green] {__version__} -> {info.tag}")
        else:
            console.print(f"[green]Steerholm is up to date:[/green] {__version__}")
        return

    # An explicit --tag is a request to install that exact version (a downgrade or
    # reinstall), so only short-circuit "up to date" when no tag was named.
    if not info.update_available and not force and tag is None:
        console.print(f"[green]Steerholm is already up to date:[/green] {__version__}")
        return

    if not yes:
        typer.confirm(f"Install Steerholm {info.tag}?", abort=True)

    try:
        run_update_installer(info.tag)
    except UpdateError as e:
        console.print(f"[bold red]Error:[/bold red] {escape(str(e))}")
        raise typer.Exit(code=1)

    console.print(f"[bold green]Updated Steerholm to {info.tag}.[/bold green]")


# ─── Servers ─────────────────────────────────────────────────────────


@add_app.command("server")
def add_server(
    name: str,
    command: Optional[str] = typer.Option(None, help="Full command to run the server (stdio)"),
    url: Optional[str] = typer.Option(None, help="Server URL (streamable HTTP)"),
    env: Optional[List[str]] = typer.Option(
        None, "--env", help="Env var for a stdio server: 'KEY=VALUE' (repeatable)"
    ),
):
    """
    Add an MCP server behind Steerholm.

    Provide --command for stdio servers or --url for HTTP servers (not both).
    Pass config/secrets to a stdio server with --env (repeatable).

    Examples:
      holm add server git --command "uvx mcp-server-git"
      holm add server db --command "uvx postgres-mcp" --env "DATABASE_URI=postgresql://..."
      holm add server remote-api --url "http://localhost:8000/mcp"
    """
    _handle(config_manager.add_server, name, command=command, url=url, env=_parse_env(env))
    console.print(f"[bold green]Added server '{escape(name)}'.[/bold green]")
    _notify_daemon_reconcile()
    console.print(f"Next: let an agent use it with [bold]holm grant <agent> {escape(name)}[/bold].")


@remove_app.command("server")
def remove_server(name: str):
    """Remove an MCP server."""
    _handle(config_manager.remove_server, name)
    console.print(f"[bold green]Removed server '{escape(name)}'.[/bold green]")
    _notify_daemon_reconcile()


def _notify_daemon_reconcile() -> None:
    """Tell a running daemon to apply server changes now (the CLI drives the
    daemon, like `docker` drives `dockerd`). No-op with a note if it's down —
    the change is persisted in config and applied when the daemon next starts.
    """
    import json
    import urllib.request
    from .config import DEFAULT_HOST, DEFAULT_PORT, get_or_create_control_token

    if not _daemon_up(DEFAULT_HOST, DEFAULT_PORT):
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
        console.print(f"[yellow]Could not reach the daemon to apply the change: {escape(str(e))}[/yellow]")
        return

    failed = result.get("failed") or []
    started = result.get("started") or []
    stopped = result.get("stopped") or []
    if failed:
        console.print(f"[bold red]Daemon could not start:[/bold red] {', '.join(escape(s) for s in failed)} (check the daemon log)")
    if started:
        console.print(f"[green]Daemon started:[/green] {', '.join(escape(s) for s in started)}")
    if stopped:
        console.print(f"[green]Daemon stopped:[/green] {', '.join(escape(s) for s in stopped)}")


def _daemon_server_status() -> Optional[dict]:
    """Fetch live per-server status from the running daemon, or None if it's down."""
    import json
    import urllib.request
    from .config import DEFAULT_HOST, DEFAULT_PORT, get_or_create_control_token

    if not _daemon_up(DEFAULT_HOST, DEFAULT_PORT):
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


@list_app.command("servers")
def list_servers():
    """List all servers with their live status."""
    servers = config_manager.list_servers()
    if not servers:
        console.print("No servers added yet. Add one with [bold]holm add server <name>[/bold].")
        return

    status = _daemon_server_status()

    table = Table(title="Servers")
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
            escape(server.name),
            escape(server.command or server.url),
            server.server_type.value,
            _status_markup(state),
            _format_uptime(st.get("uptime_seconds")),
            str(n_tools) if state == "running" else "-",
        )
    console.print(table)
    if status is None:
        console.print("[dim]Daemon not running; live status unavailable.[/dim]")


def _print_server_status(name: str) -> None:
    """Print live status + the tools a server provides (shared by `show server`)."""
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
        console.print(f"[bold red]Error:[/bold red] {escape(str(st['error']))}")

    tools = st.get("tools") or []
    if tools:
        tools_table = Table(title=f"Tools ({len(tools)})")
        tools_table.add_column("Tool", style="cyan")
        tools_table.add_column("Description", style="white")
        for tool in tools:
            desc = (tool.get("description") or "").strip().split("\n")[0]
            tools_table.add_row(escape(tool["name"]), escape(desc))
        console.print(tools_table)
    elif st["state"] == "running":
        console.print("[dim]This server exposes no tools.[/dim]")


def _print_server_grantees(name: str) -> None:
    """Print which agents have been granted access to a server."""
    grantees = []
    for agent_name in config_manager.config.agents:
        policy = config_manager.load_policy(agent_name)
        if policy and name in policy.permissions:
            tools = ", ".join(t.name for t in policy.permissions[name])
            grantees.append((agent_name, tools))
    if not grantees:
        console.print("[dim]No agents have been granted access to this server.[/dim]")
        return
    table = Table(title="Agents with access")
    table.add_column("Agent", style="cyan")
    table.add_column("Tools", style="green")
    for agent_name, tools in grantees:
        table.add_row(escape(agent_name), escape(tools))
    console.print(table)


@show_app.command("server")
def show_server(name: str):
    """Show a server: its config, live status, tools, and which agents can reach it."""
    server = config_manager.get_server(name)
    if not server:
        console.print(f"[bold red]Error:[/bold red] Server '{escape(name)}' not found.")
        raise typer.Exit(code=1)

    # escape() the user-supplied values so a '[' in a name/command/url/env key
    # isn't parsed as Rich markup (which would misrender or raise and abort).
    console.print(f"[bold]Name:[/bold] {escape(server.name)}")
    if server.command:
        console.print(f"[bold]Command:[/bold] {escape(server.command)}")
    if server.url:
        console.print(f"[bold]URL:[/bold] {escape(server.url)}")
    if server.env:
        # Show which env vars are set, but mask values so secrets don't leak.
        keys = ", ".join(f"{escape(k)}=***" for k in server.env)
        console.print(f"[bold]Env:[/bold] {keys}")
    console.print(f"[bold]Type:[/bold] {server.server_type.value}")

    _print_server_grantees(name)
    _print_server_status(name)


@app.command()
def serve(
    host: str = typer.Option(None, help="Host to bind (default: 127.0.0.1)"),
    port: int = typer.Option(None, help="Port to bind (default: 4767)"),
):
    """Start the Steerholm daemon in the foreground."""
    from .gateway import SteerholmGateway
    from .config import DEFAULT_HOST, DEFAULT_PORT
    import sys
    import logging

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    serve_host = host or DEFAULT_HOST
    serve_port = port or DEFAULT_PORT

    gateway = SteerholmGateway()
    sys.stderr.write(f"Starting Steerholm daemon (http://{serve_host}:{serve_port}/mcp)...\n")
    asyncio.run(gateway.serve(serve_host, serve_port))


# Windows runs the daemon as a per-user logon Scheduled Task (the mirror of the
# systemd --user unit on Linux and the LaunchAgent on macOS): it runs as the user
# in their session, with no admin and no stored password.
WIN_TASK_NAME = "Steerholm"


def _daemon_up(host: str, port: int, timeout: float = 1.0) -> bool:
    """True if a Steerholm daemon (not just any listener) answers on host:port.

    Probes the unauthenticated /healthz endpoint and checks the service
    signature, so a different process holding the port is not a false positive.
    """
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=timeout) as resp:
            data = json.loads(resp.read() or b"{}")
        return data.get("service") == "steerholm"
    except Exception:
        return False


@app.command()
def start():
    """Start the Steerholm daemon via the platform service manager."""
    import subprocess
    import sys

    if sys.platform == "linux":
        subprocess.run(["systemctl", "--user", "start", "steerholm"], check=True)
    elif sys.platform == "darwin":
        plist = f"{Path.home()}/Library/LaunchAgents/dev.steerholm.daemon.plist"
        subprocess.run(["launchctl", "load", plist], check=True)
    elif sys.platform == "win32":
        import time
        from .config import DEFAULT_HOST, DEFAULT_PORT
        result = subprocess.run(
            ["schtasks", "/Run", "/TN", WIN_TASK_NAME],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            console.print(f"[bold red]Error:[/bold red] {escape(result.stderr.strip() or 'Failed to start daemon.')}")
            raise typer.Exit(1)
        # /Run only triggers the task; confirm the daemon actually came up.
        for _ in range(20):
            if _daemon_up(DEFAULT_HOST, DEFAULT_PORT):
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
    """Stop the Steerholm daemon via the platform service manager."""
    import subprocess
    import sys

    if sys.platform == "linux":
        subprocess.run(["systemctl", "--user", "stop", "steerholm"], check=True)
    elif sys.platform == "darwin":
        plist = f"{Path.home()}/Library/LaunchAgents/dev.steerholm.daemon.plist"
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
            if not _daemon_up(DEFAULT_HOST, DEFAULT_PORT):
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
    """Check if the Steerholm daemon is running."""
    import subprocess
    import sys

    if sys.platform == "linux":
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "steerholm"],
            capture_output=True, text=True
        )
        state = result.stdout.strip()
        if state == "active":
            console.print("[bold green]Daemon is running.[/bold green]")
        else:
            console.print(f"[yellow]Daemon is {state}.[/yellow]")
    elif sys.platform == "darwin":
        result = subprocess.run(
            ["launchctl", "list", "dev.steerholm.daemon"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            console.print("[bold green]Daemon is running.[/bold green]")
        else:
            console.print("[yellow]Daemon is not running.[/yellow]")
    elif sys.platform == "win32":
        # Check the daemon directly (locale-proof, unlike parsing schtasks text).
        from .config import DEFAULT_HOST, DEFAULT_PORT
        if _daemon_up(DEFAULT_HOST, DEFAULT_PORT):
            console.print("[bold green]Daemon is running.[/bold green]")
        else:
            console.print("[yellow]Daemon is not running.[/yellow]")
    else:
        console.print("[bold red]Unsupported platform.[/bold red]")
        raise typer.Exit(1)


# ─── Agents ──────────────────────────────────────────────────────────


@add_app.command("agent")
def add_agent(name: str):
    """Add an agent and generate its access key."""
    access_key = _handle(config_manager.add_agent, name)
    console.print(f"[bold green]Added agent '{escape(name)}'.[/bold green]")
    console.print(f"[bold]Access key:[/bold] {access_key}")
    console.print("[yellow]Store it now — it is shown only once.[/yellow]")
    console.print(f"Next: grant it access with [bold]holm grant {escape(name)} <server>[/bold].")


@rotate_app.command("agent")
def rotate_agent(name: str):
    """Generate a new access key for an agent, keeping its grants."""
    access_key = _handle(config_manager.rotate_agent_key, name)
    console.print(f"[bold green]Rotated the access key for '{escape(name)}'.[/bold green]")
    console.print(f"[bold]New access key:[/bold] {access_key}")
    console.print("[yellow]The previous key no longer works. Update the agent's config.[/yellow]")


@list_app.command("agents")
def list_agents():
    """List all agents."""
    agents = config_manager.config.agents
    if not agents:
        console.print("No agents yet. Add one with [bold]holm add agent <name>[/bold].")
        return

    table = Table(title="Agents")
    table.add_column("Name", style="cyan")
    table.add_column("Access key", style="magenta")
    for name, agent in agents.items():
        table.add_row(escape(name), agent.key_prefix)
    console.print(table)


@remove_app.command("agent")
def remove_agent(name: str):
    """Remove an agent, its access key, and its grants."""
    _handle(config_manager.remove_agent, name)
    console.print(f"[bold green]Removed agent '{escape(name)}'.[/bold green]")


@show_app.command("agent")
def show_agent(name: str):
    """Show an agent: its access-key prefix, its grants, and how to connect."""
    agent = config_manager.get_agent(name)
    if not agent:
        console.print(f"[bold red]Error:[/bold red] Agent '{escape(name)}' not found.")
        raise typer.Exit(code=1)

    console.print(f"[bold]Agent:[/bold] {escape(agent.name)}")
    if agent.id:
        console.print(f"[bold]ID:[/bold] {agent.id}")
    console.print(f"[bold]Access key:[/bold] {agent.key_prefix}")

    policy = config_manager.load_policy(name)
    if not policy or not policy.permissions:
        console.print("[bold]Access:[/bold] [dim]none granted (default-deny)[/dim]")
    else:
        console.print("[bold]Access:[/bold]")
        for server, tools in policy.permissions.items():
            console.print(f"  [cyan]{escape(server)}[/cyan]")
            for tool in tools:
                pol_str = ""
                if tool.policies:
                    pol_str = " -> " + ", ".join(
                        f"{p.arg_name}={'re:' if p.match_type == 'regex' else ''}{p.pattern}"
                        for p in tool.policies
                    )
                console.print(f"    - [green]{escape(tool.name)}[/green]{escape(pol_str)}")

    from .config import DEFAULT_HOST, DEFAULT_PORT
    console.print(
        f"[dim]Connect: point the client at http://{DEFAULT_HOST}:{DEFAULT_PORT}/mcp "
        "with header 'Authorization: Bearer <access key>'.[/dim]"
    )


# ─── Grants ──────────────────────────────────────────────────────────


@app.command()
def grant(
    agent: str,
    server: str,
    tool: str = typer.Option("*", help="Tool name or glob pattern (default: *)"),
    args: Optional[List[str]] = typer.Option(
        None, help="Argument policies: 'arg=pattern' (glob) or 'arg=re:pattern' (regex)"
    ),
):
    """
    Grant an agent access to a server's tools.

    Examples:
      holm grant my-agent filesystem
      holm grant my-agent filesystem --tool "read_*" --args "path=/home/user/**"
      holm grant my-agent db --tool "query" --args "sql=re:^SELECT.*" "db=production"
    """
    if agent not in config_manager.config.agents:
        console.print(f"[bold red]Error:[/bold red] Agent '{escape(agent)}' not found.")
        raise typer.Exit(code=1)
    if not config_manager.get_server(server) and server != "*":
        console.print(f"[yellow]Warning: Server '{escape(server)}' is not currently added.[/yellow]")

    _handle(config_manager.grant_permission, agent, server, tool=tool, arg_policies=args)
    console.print(f"[bold green]Granted[/bold green] '{escape(agent)}' access to '{escape(server)}' tool '{escape(tool)}'.")


@app.command()
def revoke(
    agent: str,
    server: str,
    tool: Optional[str] = typer.Option(
        None, help="Revoke only this tool grant (default: all access to the server)"
    ),
):
    """
    Revoke an agent's access to a server.

    With --tool, remove only that tool grant; without it, remove the agent's
    entire access to the server. --tool matches the exact pattern you granted
    (e.g. "read_*"), not a glob expansion of it.

    Examples:
      holm revoke my-agent filesystem
      holm revoke my-agent filesystem --tool "read_*"
    """
    if agent not in config_manager.config.agents:
        console.print(f"[bold red]Error:[/bold red] Agent '{escape(agent)}' not found.")
        raise typer.Exit(code=1)

    removed = _handle(config_manager.revoke_permission, agent, server, tool=tool)
    what = f"tool '{tool}' on '{server}'" if tool else f"all access to '{server}'"
    if removed:
        console.print(f"[bold green]Revoked[/bold green] {escape(agent)}'s {escape(what)}.")
    else:
        target = f"tool '{tool}' on '{server}'" if tool else f"'{server}'"
        console.print(f"[yellow]Nothing to revoke: '{escape(agent)}' has no grant for {escape(target)}.[/yellow]")


# ─── Audit log ───────────────────────────────────────────────────────


def _iter_event_log():
    """Yield each decision event (a dict) from the durable JSONL audit log, oldest
    first. Streams the file line by line and skips blank / torn / non-object lines,
    so it tolerates a corrupted log and works with the daemon stopped."""
    import json
    from . import config
    path = config.CONFIG_DIR / "events.jsonl"
    if not path.exists():
        return
    # errors="replace" so a torn multibyte char (crash mid-write) can't abort the
    # whole read; that line then fails JSON parsing and is skipped below.
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn line from a crash mid-write
            if isinstance(obj, dict):
                yield obj  # ignore any valid-JSON non-object line


def _event_time(ts: str) -> str:
    """ISO '2026-08-31T12:04:35.221+00:00' -> '12:04:35'."""
    return ts[11:19] if len(ts) >= 19 else ts


def _decision_markup(decision: str) -> str:
    return {
        "allowed": "[green]allowed[/green]",
        "denied": "[red]denied[/red]",
        "error": "[yellow]error[/yellow]",
    }.get(decision, f"[dim]{escape(decision)}[/dim]")


def _render_event_table(events: list, title: str = "Audit log") -> None:
    table = Table(title=title)
    table.add_column("Time", style="dim")
    table.add_column("Agent", style="cyan")
    table.add_column("Server", style="magenta")
    table.add_column("Tool")
    table.add_column("Decision")
    table.add_column("Reason", style="dim")
    for e in events:
        table.add_row(
            escape(_event_time(str(e.get("ts", "")))),
            escape(str(e.get("agent", ""))),
            escape(str(e.get("server") or "-")),
            escape(str(e.get("tool", ""))),
            _decision_markup(str(e.get("decision", ""))),
            escape(str(e.get("reason") or "")),
        )
    console.print(table)


_DECISIONS = ("allowed", "denied", "error")


@app.command("log")
def audit_log(
    limit: int = typer.Option(50, "--limit", "-n", min=0, help="Show the most recent N events (0 for all)"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Only events for this agent name"),
    decision: Optional[str] = typer.Option(None, "--decision", help="Only 'allowed', 'denied', or 'error'"),
):
    """Show recorded agent tool-call decisions from the durable audit log.

    Reads the on-disk log directly, so it works with the daemon stopped.
    """
    if decision is not None and decision not in _DECISIONS:
        console.print(f"[bold red]Error:[/bold red] --decision must be one of {', '.join(_DECISIONS)}.")
        raise typer.Exit(code=1)

    from collections import deque
    # Keep only the last `limit` matching events (deque bounds memory, so a huge
    # unrotated log isn't fully loaded); limit 0 -> unbounded (show all).
    matching = deque(maxlen=limit or None)
    for e in _iter_event_log():
        if agent is not None and e.get("agent") != agent:
            continue
        if decision is not None and e.get("decision") != decision:
            continue
        matching.append(e)

    if not matching:
        console.print("[dim]No matching activity in the audit log.[/dim]")
        return
    _render_event_table(list(matching))


if __name__ == "__main__":  # pragma: no cover
    app()
