# rxyy mcp

**Remote console for coding agents.** See what every Cursor / Claude Code / Codex
agent on your desktop is doing, answer their questions, and dispatch new work —
from your phone, from anywhere.

## Why

You kick off three coding agents and leave your desk. Twenty minutes later one is
blocked on a yes/no question, one finished early, and one is quietly rewriting the
wrong file. Today the only fix is walking back to the machine.

rxyy mcp keeps a persistent console between you and your agents:

- **Live board** — every agent session is a tab, with liveness from real signals
  (file writes, IDE state, MCP heartbeats, lock renewals).
- **Ask & answer from your phone** — agents park questions here; you reply from a
  share page. The MCP request long-hangs on SSE until you answer.
- **Dispatch & takeover** — assign work to an idle agent, or let a fresh session
  take over a dead one with the same conversation id.
- **Desktop shell** — optional pywebview window: MCP console, task board,
  local git project list, settings.

## Quick start

> Windows 10/11, Python 3.11+

```powershell
git clone https://github.com/rixingyingyao/rxyy-mcp && cd rxyy-mcp
powershell -ExecutionPolicy Bypass -File .\install.ps1
rxyy-mcp hub --daemon
```

Then point your MCP client at `http://127.0.0.1:39222/mcp` and open
`http://127.0.0.1:38777/ui`.

Optional desktop window:

```powershell
rxyy-mcp console
```

Machine state defaults to `%LOCALAPPDATA%\rxyy-mcp` when `RXYY_MCP_DATA_DIR`
is unset. This package does **not** read or write an existing rxyy-tools install.

## MCP tools

- `zhi` — ask the human, long-hang until answered
- `zt`  — report status (non-blocking)
- `ji`  — memory / blackboard / agent-to-agent relay

## Tests

```powershell
pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File .\run-tests.ps1
```

## License

MIT
