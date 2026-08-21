# Tests

```bash
pytest                  # all
pytest tests/unit       # fast, no network
pytest tests/integration
pytest tests/e2e        # needs npx
```

## Unit

**[test_models.py](unit/test_models.py)** — Pydantic model validation

- Server: stdio, http, defaults, missing name
- Identity: creation, missing fields
- ArgumentPolicy: glob, regex, default type, invalid types
- ToolPermission, AgentPolicy, Config: creation, empty states
- JSON roundtrip for Server and AgentPolicy

**[test_config.py](unit/test_config.py)** — ConfigManager service methods

- Servers: add (stdio/http), validation (both/neither/duplicate), list, remove, persistence
- Identities: add, duplicate raises, remove cascades to policy, nonexistent raises
- Policies: grant_permission creates policy, argument policies (glob/regex), invalid format raises, identity not found raises, additive within server, additive across servers
- Platform: Unix vs Windows config directory

**[test_permissions.py](unit/test_permissions.py)** — PermissionEngine

- Server level: allowed, denied, unregistered
- Tool level: exact match denied, wildcard allows all, glob match/no-match
- Arguments: glob path allowed/denied, missing arg, no args when policy exists, regex allowed/denied, literal glob
- Edge cases: empty policy, empty tool list
- Multiple permissions: two patterns on same server, first-match-wins
- Regex: start-anchored only, full match with `$`
- GPARS errors: correct code (-31001), data payload, message content

**[test_errors.py](unit/test_errors.py)** — Error factories

- `authorization_denied()`: type, code, data, message
- `server_unavailable()`: type, code, data, server name in message

**[test_process_manager.py](unit/test_process_manager.py)** — Command parsing & daemon

- shlex split: simple, multi-arg, quoted paths, single word, uvx
- HarbourDaemon: init, nonexistent lookup, stop all shared

**[test_identity.py](unit/test_identity.py)** — Token resolution

- Resolves correct identity from multiple
- Returns None: unknown token, no identities, keyring error, partial token

## Integration

**[test_session.py](integration/test_session.py)** — Shared gateway server (mocked MCP servers)

- Shared process startup for stdio and HTTP docked servers
- Tool discovery: single server, multiple servers, exact filter, glob filter, server filtering
- Default deny: no policy, empty policy
- Tool calls: correct routing, argument policy allowed/denied, denied tool, unknown tool, unavailable server
- Lifecycle: shared process shutdown and multi-identity process reuse

**[test_handshake.py](integration/test_handshake.py)** — Streamable HTTP authentication and sessions

- Auth: valid token, invalid token, missing authorization, malformed authorization
- Session binding: valid initialization, identity mismatch rejection, unknown session id
- Port conflict: exits cleanly when port is in use

## E2E

**[test_e2e.py](e2e/test_e2e.py)** — Real daemon + `@modelcontextprotocol/server-everything`

- Handshake: valid token connects, invalid token rejected
- Initialize: returns capabilities and server info
- List tools: full access sees all, restricted sees filtered, no policy sees none
- Tool calls: echo returns input, get-sum computes, restricted can call echo, denied on unpermitted tool, denied on argument policy violation
- Multi-session: two clients get isolated tool sets
