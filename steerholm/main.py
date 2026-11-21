import typer
import asyncio
from pathlib import Path
from typing import List, NoReturn, Optional
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from . import __version__, admin
from .childenv import child_env
from .config import ConfigManager, EX_CONFIG, UnusableStateError
from .files import write_atomic
from .models import ServerType
from .updater import UpdateError, run_update_installer, update_binary

app = typer.Typer(
    help="Steerholm — an action plane for your agents, a control plane for you.",
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
    # Recovery paths are exempt: they are how a version mismatch gets fixed, so
    # they must run against exactly the state every other command refuses.
    # `update` matters as much as `migrate` — an update interrupted between the
    # binary swap and the migration leaves a new binary on unmigrated state, and
    # re-running `holm update` is the obvious thing to try.
    # Exempt: recovery paths (`migrate`, `update`), and the service-manager
    # commands that read no versioned state. `install.sh`'s failure message tells
    # the user to run `holm stop`, so gating it would break the path out;
    # `status` is how you diagnose a daemon that will not come up.
    #
    # `start` is NOT exempt. `systemctl start` returns as soon as a Type=simple
    # unit forks, so against a behind schema it reported "Daemon started." and
    # exited 0 while `holm serve` was already exiting. The installer never calls
    # it — it starts the service directly, after migrating.
    if ctx.invoked_subcommand not in (
        "migrate", "update", "version", "stop", "status",
    ):
        try:
            config_manager.verify_schema()
            config_manager.verify_loaded()
        except UnusableStateError as e:
            err_console.print(f"[bold red]Error:[/bold red] {escape(str(e))}")
            # `serve` is what the service manager runs — the systemd unit is
            # `ExecStart=holm serve`, Restart=on-failure. Exiting 1 there loops
            # every RestartSec forever; EX_CONFIG is the code the unit is told
            # not to restart on. Every other command is a person at a terminal.
            raise typer.Exit(
                code=EX_CONFIG if ctx.invoked_subcommand == "serve" else 1)
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
            write_atomic(path, json.dumps({"checked_at": now, "latest": latest}))
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

modify_app = typer.Typer(no_args_is_help=True, help="Change an existing server.")
app.add_typer(modify_app, name="modify")

rotate_app = typer.Typer(no_args_is_help=True, help="Rotate an agent's access key.")
app.add_typer(rotate_app, name="rotate")

# `log` is a group so it can carry `log config`/`log set`, but it still runs bare
# (`holm log`, `holm log -f`) via its invoke_without_command callback. No help=
# here on purpose: that would override the callback's docstring, which is where
# the command is actually explained.
log_app = typer.Typer(invoke_without_command=True)
app.add_typer(log_app, name="log")


def _ask_daemon(op: str, **args):
    """Send a request to the daemon, and exit with its reason if it can't answer."""
    try:
        return admin.request(op, **args)
    except (admin.DaemonNotRunning, admin.AdminError) as e:
        _daemon_failed(e)


def _daemon_failed(e: Exception) -> NoReturn:
    """Say why the daemon could not answer, and exit."""
    if isinstance(e, admin.DaemonNotRunning):
        console.print("[bold red]Error:[/bold red] The daemon is not running. "
                      "Start it with [bold]holm start[/bold].")
    else:
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
        # The binary can be current while the state is not. An interrupted
        # migration leaves exactly that, and returning here would strand the
        # user: every other command refuses and points at the installer, while
        # this one reports all is well. The running binary is the new one, so it
        # has the steps — unlike the installer, which runs the OLD binary.
        if _state_is_behind():
            from .config import DEFAULT_HOST, DEFAULT_PORT

            console.print("Its stored data is still behind; finishing that now.")
            if _port_in_use(DEFAULT_HOST, DEFAULT_PORT):
                console.print(
                    "[bold red]Error:[/bold red] something is listening on "
                    f"{DEFAULT_HOST}:{DEFAULT_PORT}. If that is the Steerholm "
                    "daemon, stop it with 'holm stop', then run this again."
                )
                raise typer.Exit(code=1)
            _migrate_now()
        return

    if not yes:
        typer.confirm(f"Install Steerholm {info.tag}?", abort=True)

    # The installer replaces the binary this process is running from. A frozen
    # build reads every module it has not loaded yet from that file, so the
    # first new import after the swap reads the NEW build's archive at the old
    # one's offsets and dies with zlib.error — after a successful update. Rich
    # rendering markup was that import, and the interpreter's own shutdown
    # would be another. So: plain writes, then leave before anything loads.
    import os
    import sys
    try:
        run_update_installer(info.tag)
    except UpdateError as e:
        code, line = 1, f"Error: {e}\n"
    else:
        code, line = 0, f"Updated Steerholm to {info.tag}.\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    os._exit(code)


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
    Connect a resource to Steerholm through its MCP server.

    Provide --command for stdio servers or --url for HTTP servers (not both).
    Pass config/secrets to a stdio server with --env (repeatable).

    Examples:
      holm add server git --command "uvx mcp-server-git"
      holm add server db --command "uvx postgres-mcp" --env "DATABASE_URI=postgresql://..."
      holm add server remote-api --url "http://localhost:8000/mcp"
    """
    plan = _ask_daemon("add_server", name=name, command=command, url=url,
                       env=_parse_env(env))
    console.print(f"[bold green]Added server '{escape(name)}'.[/bold green]")
    _print_plan(plan)
    console.print(f"Next: let an agent use it with [bold]holm grant <agent> {escape(name)}[/bold].")


@modify_app.command("server")
def modify_server(
    name: str,
    command: Optional[str] = typer.Option(None, help="New launch command (stdio)"),
    url: Optional[str] = typer.Option(None, help="New server URL (streamable HTTP)"),
    env: Optional[List[str]] = typer.Option(
        None, "--env", help="Set an env var: 'KEY=VALUE' (repeatable; merges)"),
    unset: Optional[List[str]] = typer.Option(
        None, "--unset", help="Remove an env var by name (repeatable)"),
):
    """
    Change a server without removing it.

    Only what you name changes: --env merges into the existing variables and
    --unset removes them. The server keeps its id and every agent's grants, which
    is what makes this different from removing and re-adding it.

    Examples:
      holm modify server git --command "uvx mcp-server-git --repository /srv/app"
      holm modify server db --env "DATABASE_URI=postgresql://..." --unset OLD_VAR
      holm modify server api --url "http://localhost:9000/mcp"
    """
    if command is None and url is None and not env and not unset:
        console.print("[bold red]Error:[/bold red] Give at least one change to make.")
        raise typer.Exit(code=1)

    changed = _ask_daemon("modify_server", name=name, command=command, url=url,
                          env=_parse_env(env), unset=list(unset or []))
    before, after = changed["before"], changed["after"]

    # Say what a transport switch discarded.
    if url is not None and before["type"] == ServerType.stdio.value:
        count = before["env_count"]
        dropped = f" and {count} env var{'' if count == 1 else 's'}" if count else ""
        console.print(f"[yellow]Switching '{escape(name)}' to a remote URL dropped its "
                      f"launch command{dropped}.[/yellow]")
    if command is not None and before["type"] == ServerType.http.value:
        console.print(f"[yellow]Switching '{escape(name)}' to a launch command dropped "
                      f"its URL.[/yellow]")

    console.print(f"[bold green]Modified server '{escape(name)}'.[/bold green]")
    for label, key in (("Command", "command"), ("URL", "url")):
        if before[key] != after[key]:
            console.print(f"  [bold]{label}:[/bold] {escape(before[key] or '(none)')} "
                          f"-> {escape(after[key] or '(none)')}")
    if changed["env"]:
        marks = ", ".join(("+" if e["set"] else "-") + escape(e["key"]) for e in changed["env"])
        console.print(f"  [bold]Env:[/bold] {marks}")

    grantees = changed["grantees"]
    if grantees:
        console.print(
            f"[yellow]Note:[/yellow] {len(grantees)} agent"
            f"{'' if len(grantees) == 1 else 's'} still "
            f"{'has' if len(grantees) == 1 else 'have'} grants on '{escape(name)}': "
            f"{', '.join(escape(g) for g in grantees)}."
        )
    _print_plan(changed["plan"])


@remove_app.command("server")
def remove_server(name: str):
    """Remove an MCP server, and every agent's grants on it.

    Grants go with the server: a name can be reused, so a grant that outlived it
    would silently apply to whatever is added under that name next. To change a
    server without losing its grants, use `holm modify server`.
    """
    removed = _ask_daemon("remove_server", name=name)
    affected = removed["revoked"]
    console.print(f"[bold green]Removed server '{escape(name)}'.[/bold green]")
    if affected:
        console.print(
            f"Also revoked grants on '{escape(name)}' for {len(affected)} agent"
            f"{'' if len(affected) == 1 else 's'}: "
            f"{', '.join(escape(a) for a in affected)}."
        )
    for agent in removed["unreadable"]:
        console.print(
            f"[yellow]Could not read the policy for '{escape(agent)}' to revoke its grants "
            f"on '{escape(name)}'; any it holds no longer apply to anything.[/yellow]"
        )
    _print_plan(removed["plan"])


def _print_plan(plan: dict) -> None:
    """Report what the daemon is doing about a change. It applies the change
    in the background — starting a server can take a while — so this is what
    it is doing, not how it ended."""
    stopping = plan.get("stopping") or []
    starting = plan.get("starting") or []
    if stopping:
        console.print(f"[green]Daemon is stopping:[/green] {', '.join(escape(s) for s in stopping)}")
    if starting:
        console.print(f"[green]Daemon is starting:[/green] {', '.join(escape(s) for s in starting)}")
        console.print(f"See when it is up with [bold]holm show server {escape(starting[0])}[/bold].")


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
        "starting": "[cyan]starting[/cyan]",
        "failed": "[red]failed[/red]",
        "stopped": "[yellow]stopped[/yellow]",
    }.get(state, "[dim]unknown[/dim]")


@list_app.command("servers")
def list_servers():
    """List all servers with their live status."""
    servers = _ask_daemon("list_servers")
    if not servers:
        console.print("No servers added yet. Add one with [bold]holm add server <name>[/bold].")
        return

    table = Table(title="Servers")
    table.add_column("Name", style="cyan")
    table.add_column("Command", style="magenta")
    table.add_column("Type", style="green")
    table.add_column("Status")
    table.add_column("Uptime", style="dim")
    table.add_column("Tools", justify="right")
    for server in servers:
        st = server["status"] or {}
        state = st.get("state", "stopped")
        table.add_row(
            escape(server["name"]),
            escape(server["command"] or server["url"]),
            server["type"],
            _status_markup(state),
            _format_uptime(st.get("uptime_seconds")),
            str(len(st.get("tools", []))) if state == "running" else "-",
        )
    console.print(table)


def _print_server_status(st: Optional[dict]) -> None:
    """Print live status + the tools a server provides (shared by `show server`)."""
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


def _print_server_grantees(grantees: list) -> None:
    """Print which agents have been granted access to a server."""
    if not grantees:
        console.print("[dim]No agents have been granted access to this server.[/dim]")
        return
    table = Table(title="Agents with access")
    table.add_column("Agent", style="cyan")
    table.add_column("Tools", style="green")
    for grantee in grantees:
        table.add_row(escape(grantee["agent"]), escape(", ".join(grantee["tools"])))
    console.print(table)


@show_app.command("server")
def show_server(name: str):
    """Show a server: its config, live status, tools, and which agents can reach it."""
    server = _ask_daemon("show_server", name=name)

    # escape() the user-supplied values so a '[' in a name/command/url/env key
    # isn't parsed as Rich markup (which would misrender or raise and abort).
    console.print(f"[bold]Name:[/bold] {escape(server['name'])}")
    if server["id"]:
        console.print(f"[bold]ID:[/bold] {server['id']}")
    if server["command"]:
        console.print(f"[bold]Command:[/bold] {escape(server['command'])}")
    if server["url"]:
        console.print(f"[bold]URL:[/bold] {escape(server['url'])}")
    if server["env_keys"]:
        # Which env vars are set; the daemon never sends their values.
        keys = ", ".join(f"{escape(k)}=***" for k in server["env_keys"])
        console.print(f"[bold]Env:[/bold] {keys}")
    console.print(f"[bold]Type:[/bold] {server['type']}")

    _print_server_grantees(server["grantees"])
    _print_server_status(server["status"])


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
    try:
        asyncio.run(gateway.serve(serve_host, serve_port))
    except UnusableStateError as e:
        # Diagnosable, not a crash. The systemd unit and the Windows logon task
        # both restart on failure, so a traceback here would loop every few
        # seconds instead of reporting once.
        err_console.print(f"[bold red]Error:[/bold red] {escape(str(e))}")
        raise typer.Exit(code=1)


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


def _port_in_use(host: str, port: int) -> bool:
    """Whether anything at all holds host:port.

    `_daemon_up` answers a different question and answers it optimistically: a
    daemon busy spawning stdio servers reads as absent. That is the right trade
    for `status` and `stop`; it is the wrong one for `migrate`, where guessing
    "down" means moving config.json out from under a live daemon.

    Asked by binding the port, not connecting to it. A connect only answers
    clearly where a closed port is refused at once: Windows retries for about
    two seconds first, which read as "occupied" and stopped every Windows
    install at migrate. A bind is answered by the OS without waiting, and a
    daemon too busy to answer still holds the port.
    """
    import socket
    import sys
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if sys.platform == "win32":
            # Otherwise Windows lets a specific address bind beside a listener
            # on the wildcard address, and the port reads as free.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            # Connections from a daemon just stopped linger in TIME_WAIT; they
            # do not hold the port, and without this they would read as if
            # they did. A listener still refuses the bind.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


@app.command()
def start():
    """Start the Steerholm daemon via the platform service manager."""
    import subprocess
    import sys

    if sys.platform == "linux":
        subprocess.run(["systemctl", "--user", "start", "steerholm"], check=True, env=child_env())
    elif sys.platform == "darwin":
        plist = f"{Path.home()}/Library/LaunchAgents/dev.steerholm.daemon.plist"
        subprocess.run(["launchctl", "load", plist], check=True, env=child_env())
    elif sys.platform == "win32":
        import time
        from .config import DEFAULT_HOST, DEFAULT_PORT
        result = subprocess.run(
            ["schtasks", "/Run", "/TN", WIN_TASK_NAME],
            capture_output=True, text=True, env=child_env(),
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


@app.command(hidden=True)
def migrate():
    """Bring the on-disk state up to this build's schema.

    Hidden: the installer runs this as a post-install step, and a user should
    never need to think about it. Documented for contributors, who install from
    source and so never go through the installer.
    """
    from . import migrations
    from .config import CONFIG_DIR, DEFAULT_HOST, DEFAULT_PORT

    # The swap moves config.json aside and back; a daemon reloading in that
    # window reads an absent config, denies every agent, and tears down every
    # running server on the next reconcile. The installer stops the daemon for
    # exactly this reason — a direct run has to check for itself.
    if _port_in_use(DEFAULT_HOST, DEFAULT_PORT):
        console.print(
            f"[bold red]Error:[/bold red] something is listening on "
            f"{DEFAULT_HOST}:{DEFAULT_PORT}. If that is the Steerholm daemon, "
            "stop it with 'holm stop', then migrate."
        )
        raise typer.Exit(code=1)

    _migrate_now()


def _state_is_behind() -> bool:
    """Whether the on-disk state is older than this build understands."""
    from . import migrations
    from .config import CONFIG_DIR

    try:
        return migrations.resolve_version(
            migrations.MigrationStore(CONFIG_DIR)) < migrations.CURRENT_VERSION
    except migrations.store.StateError:
        return True     # unreadable; migrate will say why


def _migrate_now() -> None:
    """Bring the state up to date, reporting what happened. Exits on failure."""
    from . import migrations
    from .config import CONFIG_DIR

    try:
        applied = migrations.run(CONFIG_DIR)
    except migrations.MigrationError as e:
        console.print(f"[bold red]Error:[/bold red] {escape(str(e))}")
        raise typer.Exit(code=1)

    if applied:
        console.print(
            f"[bold green]Migrated[/bold green] {escape(str(CONFIG_DIR))} "
            f"to schema v{migrations.CURRENT_VERSION} "
            f"({applied} step{'s' if applied != 1 else ''})."
        )
        # Say where the originals went, and what they are for. Otherwise the
        # only mention of the backup is a docs page, and the installer is what
        # usually runs this.
        kept = migrations.backup_path(CONFIG_DIR)
        if kept.exists():
            console.print(
                f"The state as it was before is at {escape(str(kept))}, so you "
                "can compare if anything looks wrong. It holds a copy of your "
                "config, secrets included, and the next migration replaces it."
            )
    else:
        console.print(
            f"[green]Already at schema v{migrations.CURRENT_VERSION}.[/green]"
        )


@app.command()
def stop():
    """Stop the Steerholm daemon via the platform service manager."""
    import subprocess
    import sys

    if sys.platform == "linux":
        subprocess.run(["systemctl", "--user", "stop", "steerholm"], check=True, env=child_env())
    elif sys.platform == "darwin":
        plist = f"{Path.home()}/Library/LaunchAgents/dev.steerholm.daemon.plist"
        subprocess.run(["launchctl", "unload", plist], check=True, env=child_env())
    elif sys.platform == "win32":
        import time
        import getpass
        from .config import DEFAULT_HOST, DEFAULT_PORT
        # /End fails harmlessly when nothing is running; the port is the source of
        # truth, so an already-stopped daemon is reported as stopped (exit 0).
        subprocess.run(
            ["schtasks", "/End", "/TN", WIN_TASK_NAME],
            capture_output=True, text=True, env=child_env(),
        )
        # holmd.exe is a single-file build: a launcher, and the process it starts
        # that actually serves the port. /End ends the launcher alone and left
        # the daemon running. This user's holmd.exe processes, with everything
        # they started (/T — the MCP servers), are the daemon.
        subprocess.run(
            ["taskkill", "/F", "/T", "/FI", f"USERNAME eq {getpass.getuser()}",
             "/IM", "holmd.exe"],
            capture_output=True, text=True, env=child_env(),
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
            capture_output=True, text=True, env=child_env(),
        )
        state = result.stdout.strip()
        if state == "active":
            console.print("[bold green]Daemon is running.[/bold green]")
        else:
            console.print(f"[yellow]Daemon is {state}.[/yellow]")
    elif sys.platform == "darwin":
        result = subprocess.run(
            ["launchctl", "list", "dev.steerholm.daemon"],
            capture_output=True, text=True, env=child_env(),
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
    access_key = _ask_daemon("add_agent", name=name)["key"]
    console.print(f"[bold green]Added agent '{escape(name)}'.[/bold green]")
    console.print(f"[bold]Access key:[/bold] {access_key}")
    console.print("[yellow]Store it now — it is shown only once.[/yellow]")
    console.print(f"Next: grant it access with [bold]holm grant {escape(name)} <server>[/bold].")


@rotate_app.command("agent")
def rotate_agent(name: str):
    """Generate a new access key for an agent, keeping its grants."""
    access_key = _ask_daemon("rotate_agent", name=name)["key"]
    console.print(f"[bold green]Rotated the access key for '{escape(name)}'.[/bold green]")
    console.print(f"[bold]New access key:[/bold] {access_key}")
    console.print("[yellow]The previous key no longer works. Update the agent's config.[/yellow]")


@list_app.command("agents")
def list_agents():
    """List all agents."""
    agents = _ask_daemon("list_agents")
    if not agents:
        console.print("No agents yet. Add one with [bold]holm add agent <name>[/bold].")
        return

    table = Table(title="Agents")
    table.add_column("Name", style="cyan")
    table.add_column("Access key", style="magenta")
    for agent in agents:
        table.add_row(escape(agent["name"]), agent["key_prefix"])
    console.print(table)


@remove_app.command("agent")
def remove_agent(name: str):
    """Remove an agent, its access key, and its grants."""
    _ask_daemon("remove_agent", name=name)
    console.print(f"[bold green]Removed agent '{escape(name)}'.[/bold green]")


@show_app.command("agent")
def show_agent(name: str):
    """Show an agent: its access-key prefix, its grants, and how to connect."""
    agent = _ask_daemon("show_agent", name=name)

    console.print(f"[bold]Agent:[/bold] {escape(agent['name'])}")
    if agent["id"]:
        console.print(f"[bold]ID:[/bold] {agent['id']}")
    console.print(f"[bold]Access key:[/bold] {agent['key_prefix']}")

    if not agent["access"]:
        console.print("[bold]Access:[/bold] [dim]none granted (default-deny)[/dim]")
    else:
        console.print("[bold]Access:[/bold]")
        for grant in agent["access"]:
            console.print(f"  [cyan]{escape(grant['server'])}[/cyan]")
            for tool in grant["tools"]:
                pol_str = ""
                if tool["policies"]:
                    pol_str = " -> " + ", ".join(
                        f"{p['arg']}={'re:' if p['type'] == 'regex' else ''}{p['pattern']}"
                        for p in tool["policies"]
                    )
                console.print(f"    - [green]{escape(tool['name'])}[/green]{escape(pol_str)}")

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
        None, help="Argument policy: 'arg=pattern' (glob) or 'arg=re:pattern' (regex); repeat for more"
    ),
):
    """
    Grant an agent access to a server's tools.

    Examples:
      holm grant my-agent filesystem
      holm grant my-agent filesystem --tool "read_*" --args "path=/home/user/**"
      holm grant my-agent db --tool "query" --args "sql=re:^SELECT.*" --args "db=production"
    """
    _ask_daemon("grant", agent=agent, server=server, tool=tool, args=list(args or []))
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
    removed = _ask_daemon("revoke", agent=agent, server=server, tool=tool)["removed"]
    what = f"tool '{tool}' on '{server}'" if tool else f"all access to '{server}'"
    if removed:
        console.print(f"[bold green]Revoked[/bold green] {escape(agent)}'s {escape(what)}.")
    else:
        target = f"tool '{tool}' on '{server}'" if tool else f"'{server}'"
        console.print(f"[yellow]Nothing to revoke: '{escape(agent)}' has no grant for {escape(target)}.[/yellow]")


# ─── Audit log ───────────────────────────────────────────────────────


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


def _event_line(e: dict) -> str:
    """One aligned line for a decision event (used by --follow)."""
    reason = str(e.get("reason") or "")
    if len(reason) > 60:
        reason = reason[:60] + "…"
    return (
        f"[dim]{escape(_event_time(str(e.get('ts', ''))))}[/dim]  "
        f"[cyan]{escape(str(e.get('agent', '')))}[/cyan]  "
        f"[magenta]{escape(str(e.get('server') or '-'))}[/magenta]  "
        f"{escape(str(e.get('tool', '')))}  "
        f"{_decision_markup(str(e.get('decision', '')))}"
        + (f"  [dim]{escape(reason)}[/dim]" if reason else "")
    )


def _limit_text(value: int, unit: str) -> str:
    return f"{value} {unit}" if value else "unlimited"


def _retention_summary(audit: dict) -> str:
    return (f"at most {_limit_text(audit['max_files'], 'files')}, "
            f"kept for {_limit_text(audit['max_age_days'], 'days')}")


@log_app.command("config")
def log_config():
    """Show how much history the log keeps, and where it lives.

    Read-only — change these with `holm log set`.
    """
    audit = _ask_daemon("log_config")
    per_file = audit["segment_bytes"] // (1024 * 1024)

    console.print("[bold]Audit log[/bold]")
    # soft_wrap so a long path is never broken mid-filename (it must stay
    # copy-pasteable into grep/jq).
    console.print(f"  Files:         {escape(audit['files'])}", soft_wrap=True)
    console.print(f"  Max files:     {audit['max_files'] or 'unlimited'}")
    console.print(f"  Max age:       {_limit_text(audit['max_age_days'], 'days')}")
    total = per_file * audit["max_files"]
    console.print(
        f"  Disk ceiling:  {str(total) + ' MB' if total else 'unbounded (no file limit)'}"
        f" [dim]({per_file} MB per file)[/dim]")
    console.print("\n[dim]Change it with [bold]holm log set[/bold], e.g. "
                  "[bold]holm log set --max-files 10 --max-age-days 30[/bold].[/dim]")


@log_app.command("set")
def log_set(
    max_files: Optional[int] = typer.Option(
        None, "--max-files", help="How many log files to keep (0 = no limit)"),
    max_age_days: Optional[int] = typer.Option(
        None, "--max-age-days", help="Delete log files older than this (0 = no limit)"),
):
    """Change how much history the log keeps.

    Both limits apply: a log file is removed once it is past the file count OR
    older than the age limit — the count bounds disk use, the age sets how long
    history lives. See the current values with `holm log config`.

    Examples:
      holm log set --max-age-days 30    # keep a month of history
      holm log set --max-files 20       # allow more history on disk
      holm log set --max-age-days 0     # no age limit (count still applies)
    """
    if max_files is None and max_age_days is None:
        console.print("[bold red]Error:[/bold red] Give at least one setting to change.")
        raise typer.Exit(code=1)
    audit = _ask_daemon("log_set", max_files=max_files, max_age_days=max_age_days)
    console.print("[bold green]Updated the audit log's retention.[/bold green]")
    console.print(f"Keeping {_retention_summary(audit)}.")


def _warn_if_name_is_ambiguous(kind: str, name: Optional[str], ids) -> None:
    """Say so when a filtered name covers more than one principal.

    Takes the set of agent ids seen for that name, so the caller can collect them
    in its existing pass instead of materialising the whole log.

    Removing and re-adding an agent reuses the name for a different principal, so
    a name filter can silently mix two agents' histories. Point at the ids, which
    are what separate them.
    """
    if not name:
        return
    # Only real ids count. A missing id means the agent was already gone when the
    # call was adjudicated, which is not evidence of a second principal.
    ids = {i for i in ids if i}
    if len(ids) < 2:
        return
    shown = ", ".join(escape(str(i)) for i in sorted(ids))
    console.print(
        f"[yellow]Note:[/yellow] {len(ids)} different {kind}s have used the name "
        f"'{escape(name)}' (removed and re-added). Filter by id to separate them: {shown}"
    )


@log_app.callback(invoke_without_command=True)
def audit_log(
    ctx: typer.Context,
    number: int = typer.Option(50, "--number", "-n", min=0, help="Show the most recent N entries (0 for all)"),
    agent: Optional[str] = typer.Option(
        None, "--agent", help="Only this agent, by name or id"),
    server: Optional[str] = typer.Option(
        None, "--server", help="Only this server, by name or id"),
    status: Optional[str] = typer.Option(
        None, "--status", help="Only this outcome: allowed, denied, or error"),
    follow: bool = typer.Option(
        False, "--follow", "-f", help="Watch new calls as they are recorded (Ctrl-C to stop)"),
):
    """
    Show what your agents did and whether it was allowed.

    Steerholm records every tool call an agent makes through it — which agent,
    which server, which tool, and whether policy allowed it, denied it, or the
    server failed. The daemon reads that record for you.

    Filters combine, and --agent takes a name or an agent id (`holm show agent`
    prints the id). A name can be reused by removing and re-adding an agent, so
    filtering by id is what isolates one specific agent's history.

    Examples:
      holm log                                    # the 50 most recent calls
      holm log --status denied                    # only what was refused
      holm log --server git --agent coding-agent  # one agent, on one server
      holm log --follow                           # watch calls as they happen
    """
    if ctx.invoked_subcommand is not None:
        # A subcommand (e.g. `holm log config`) handles it. These options belong to
        # `holm log` itself, so reject them here rather than silently dropping them.
        # Derived from the callback's own parameters, so a renamed or newly added
        # option cannot silently escape the check. The source is compared by name
        # rather than importing click's enum: click is only transitive here (and
        # newer typer vendors its own copy, which would never compare equal).
        given = [p.name for p in ctx.command.params
                 if p.name != "help"
                 and getattr(ctx.get_parameter_source(p.name), "name", "DEFAULT") != "DEFAULT"]
        if given:
            console.print(
                f"[bold red]Error:[/bold red] {', '.join('--' + g for g in given)} "
                f"appl{'ies' if len(given) == 1 else 'y'} to [bold]holm log[/bold], "
                f"not [bold]holm log {ctx.invoked_subcommand}[/bold]."
            )
            raise typer.Exit(code=1)
        return
    filters = {"number": number, "agent": agent, "server": server, "status": status}
    if follow:
        _follow_log(filters, agent, server)
        return
    history = _ask_daemon("log", **filters)
    if not history["events"]:
        console.print("[dim]No matching activity in the audit log.[/dim]")
        return
    _render_event_table(history["events"])
    # Checked over the whole history, not just the shown window, or narrowing
    # with -n would hide the very conflation this warns about.
    _warn_if_name_is_ambiguous("agent", agent, history["agent_ids"])
    _warn_if_name_is_ambiguous("server", server, history["server_ids"])


def _follow_log(filters: dict, agent: Optional[str], server: Optional[str]) -> None:
    """Print the recent history, then each new decision as the daemon records it.
    The daemon hands over from one to the other, so nothing repeats or slips
    through between them."""
    results = admin.stream("log_follow", **filters)
    try:
        history = next(results, None)
        if history is None:
            raise admin.AdminError("The daemon hung up without answering.")
        for e in history["events"]:
            console.print(_event_line(e))
        _warn_if_name_is_ambiguous("agent", agent, history["agent_ids"])
        _warn_if_name_is_ambiguous("server", server, history["server_ids"])
        console.print("[dim]Watching for new decisions… (Ctrl-C to stop)[/dim]")
        for e in results:
            console.print(_event_line(e))
        console.print("[yellow]The daemon stopped, so this stopped following.[/yellow]")
    except KeyboardInterrupt:
        pass
    except (admin.DaemonNotRunning, admin.AdminError) as e:
        _daemon_failed(e)
    finally:
        results.close()


if __name__ == "__main__":  # pragma: no cover
    app()
