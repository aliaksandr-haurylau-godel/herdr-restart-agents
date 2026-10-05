#!/usr/bin/env python3
"""Restart coding agents in their Herdr panes, resuming the same session.

Flow per pane: read the agent's launch command and session id, ask the agent to
exit, wait until the pane is back at the shell, then run the same command with
`--resume <session id>` (or the agent's own resume flag).
"""
import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
PLUGIN_ID = "restart-agents"
EXIT_TIMEOUT = 20
PARALLEL = 6

# agent kind -> (resume flag template, how to quit). Quit "slash" types /exit,
# "ctrlc" presses ctrl+c twice.
RESUME = {
    "claude": (["--resume", "{id}"], "slash"),
    "codex": (None, "ctrlc"),          # `codex resume <id>` is a subcommand
    "agy": (["--conversation", "{id}"], "ctrlc"),
    "opencode": (["--session", "{id}"], "ctrlc"),
    "cursor": (["--resume", "{id}"], "ctrlc"),
    "grok": (["--resume", "{id}"], "ctrlc"),
    "hermes": (["--resume", "{id}"], "ctrlc"),
}
# Flags that select a session; dropped from the old command before resuming.
SESSION_FLAGS = {"--resume", "-r", "--continue", "-c", "--session", "--conversation", "--session-id"}
SKIP_STATES = {"working", "blocked"}


def herdr(*args, check=True):
    p = subprocess.run([HERDR, *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"herdr {' '.join(args)}: {p.stderr.strip() or p.stdout.strip()}")
    return json.loads(p.stdout) if p.stdout.strip().startswith("{") else p.stdout


def list_agents():
    return herdr("agent", "list")["result"]["agents"]


def is_agent_proc(proc, kind):
    # Some foreground processes (npm exec wrappers) carry no "argv" key.
    argv = proc.get("argv") or []
    return proc.get("argv0") == kind or (bool(argv) and os.path.basename(argv[0]) == kind)


def launch_argv(pane, kind):
    info = herdr("pane", "process-info", "--pane", pane)["result"]["process_info"]
    for proc in info["foreground_processes"]:
        if is_agent_proc(proc, kind) and proc.get("argv"):
            return proc["argv"], info.get("shell_pid")
    return None, info.get("shell_pid")


def strip_session_flags(argv):
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in SESSION_FLAGS:
            skip = a != "--continue" and a != "-c"
            continue
        if any(a.startswith(f + "=") for f in SESSION_FLAGS if f.startswith("--")):
            continue
        out.append(a)
    return out


def build_command(agent, argv):
    kind = agent["agent"]
    session = agent.get("agent_session", {}).get("value")
    if kind not in RESUME or not session:
        raise RuntimeError(f"no resume rule or session id for '{kind}'")
    flags, _ = RESUME[kind]
    if kind == "codex":
        return ["codex", "resume", session]
    base = strip_session_flags(argv or [kind])
    return base + [f.format(id=session) for f in flags]


def at_shell(pane, kind):
    info = herdr("pane", "process-info", "--pane", pane)["result"]["process_info"]
    return not any(is_agent_proc(p, kind) for p in info["foreground_processes"])


def restart(agent):
    pane, kind = agent["pane_id"], agent["agent"]
    label = agent.get("terminal_title_stripped") or pane
    if agent["agent_status"] in SKIP_STATES:
        return pane, label, "skipped", f"agent is {agent['agent_status']}"
    try:
        argv, _ = launch_argv(pane, kind)
        cmd = build_command(agent, argv)
        quit_mode = RESUME[kind][1]
        if quit_mode == "slash":
            herdr("pane", "send-text", pane, "/exit")
            herdr("pane", "send-keys", pane, "enter")
        else:
            herdr("pane", "send-keys", pane, "ctrl+c")
            time.sleep(0.4)
            herdr("pane", "send-keys", pane, "ctrl+c")
        deadline = time.time() + EXIT_TIMEOUT
        while time.time() < deadline:
            if at_shell(pane, kind):
                break
            time.sleep(0.5)
        else:
            return pane, label, "failed", f"agent did not exit within {EXIT_TIMEOUT}s"
        herdr("pane", "run", pane, shlex.join(cmd))
        return pane, label, "restarted", shlex.join(cmd)
    except Exception as e:  # report per pane, keep going with the others
        return pane, label, "failed", str(e)


def restart_many(agents):
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        return list(ex.map(restart, agents))


def print_results(results):
    for pane, label, status, detail in results:
        print(f"{status:10} {pane:8} {label}  {detail if status != 'restarted' else ''}")
    n = sum(r[2] == "restarted" for r in results)
    print(f"\nrestarted {n} of {len(results)}")


def focused_pane():
    ctx = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if ctx:
        try:
            c = json.loads(ctx)
            for key in ("pane_id", "focused_pane_id"):
                if c.get(key):
                    return c[key]
            if isinstance(c.get("pane"), dict) and c["pane"].get("pane_id"):
                return c["pane"]["pane_id"]
        except ValueError:
            pass
    for a in list_agents():
        if a.get("focused"):
            return a["pane_id"]
    return None


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "current"
    if mode == "current":
        pane = focused_pane()
        agent = next((a for a in list_agents() if a["pane_id"] == pane), None)
        if not agent:
            print("no agent in the focused pane")
            return 1
        print_results([restart(agent)])
    elif mode == "open-confirm":
        herdr("plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "confirm-all")
    elif mode == "all-ui":
        agents = list_agents()
        idle = [a for a in agents if a["agent_status"] not in SKIP_STATES]
        busy = len(agents) - len(idle)
        print(f"Agents: {len(agents)}; will restart: {len(idle)}; skipped (working/blocked): {busy}")
        if input("Restart all? [y/N] ").strip().lower() != "y":
            return 0
        print_results(restart_many(agents))
        input("\nEnter to close")
    return 0


if __name__ == "__main__":
    sys.exit(main())
