# Contributing to Steerholm

## Development Setup

```bash
git clone https://github.com/steerholm/steerholm.git
cd steerholm
uv venv
source .venv/bin/activate    # or .venv\Scripts\activate on Windows
uv pip install -e ".[dev]"
```

## Running Tests

```bash
pytest
```

## Project Structure

```
steerholm/
├── main.py              # CLI entry point (holm)
├── models.py            # Pydantic data models
├── config.py            # ConfigManager, file paths, platform detection
├── process_manager.py   # ServerProcess, SteerholmDaemon
├── gateway.py           # SteerholmGateway, Streamable HTTP endpoint and routing
├── permissions.py       # PermissionEngine, policy matching
└── errors.py            # Structured error codes (AUTHORIZATION_DENIED, SERVER_UNAVAILABLE)
```

## Architecture

The key architectural rule: agents never talk directly to MCP servers. All traffic flows through Steerholm, which authenticates the agent and enforces the user's security policy.

Agents connect to the daemon's Streamable HTTP MCP endpoint. Users manage servers, agents, policies, and daemon lifecycle through the `holm` admin CLI.

## What We're Looking For

- Bug fixes and reliability improvements
- Additional policy match types (beyond glob and regex)
- Performance improvements to the proxy layer
- Test coverage for edge cases
- Documentation improvements

## Guidelines

- Run `pytest` before submitting. All tests must pass.
- Policy enforcement is default-deny. No code path should allow access without an explicit policy check.
- Errors returned to agents must use Steerholm's error codes (`-31001` for `AUTHORIZATION_DENIED`, `-31002` for `SERVER_UNAVAILABLE`).
- Don't leak policy details in error messages. The agent should know *what* was denied, not *what would be allowed*.
