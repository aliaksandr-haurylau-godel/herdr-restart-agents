# herdr-restart-agents

A Herdr plugin that restarts coding agents in their panes and resumes the same session.

## Actions

- `restart-agents.restart-current` restarts the agent in the focused pane.
- `restart-agents.restart-all` opens a confirmation window, then restarts every agent. Agents in the `working` or `blocked` state are skipped.

For each pane the plugin reads the agent's launch command and session id, asks the agent to exit, waits until the pane is back at the shell, then runs the same command with the agent's resume flag. Any session-selecting flags in the old command (`--resume`, `-r`, `--continue`, `-c`, `--session`, `--conversation`, `--session-id`) are dropped first; other flags such as `-n <name>` are kept.

## Install

```sh
herdr plugin install aliaksandr-haurylau-godel/herdr-restart-agents
```

## Key bindings

Add to `config.toml`, then run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+r"
type = "plugin_action"
command = "restart-agents.restart-current"
description = "restart agent in focused pane"

[[keys.command]]
key = "prefix+shift+r"
type = "plugin_action"
command = "restart-agents.restart-all"
description = "restart all agents"
```

Check that these keys are not already used in your config.

## Supported agents

| Agent | Resume command | Exit |
|---|---|---|
| Claude Code (`claude`) | `--resume <id>` | `/exit` |
| Antigravity (`agy`) | `--conversation <id>` | Ctrl+C twice |
| OpenCode (`opencode`) | `--session <id>` | Ctrl+C twice |
| Codex | `codex resume <id>` | Ctrl+C twice |
| Cursor, Grok, Hermes | `--resume <id>` | Ctrl+C twice |

## Limitations

- Tested on Claude Code (including sessions started with `--session-id`), Antigravity and OpenCode. The other agents in the table were not tested.
- `restart-all` as a whole and its confirmation window were not tested, and neither were the key bindings above. The per-pane restart that both actions share was tested.
- After a restart, an agent name set with `herdr agent start` is lost; address the agent by pane id.
- Agents in the `working` or `blocked` state are skipped by `restart-all`.

## License

MIT
