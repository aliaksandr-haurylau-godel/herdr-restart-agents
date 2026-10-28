#!/usr/bin/env python3
"""Restart coding agents in their Herdr panes, resuming the same session.

Flow per pane: read the agent's launch command and session id, ask the agent to
exit, wait until the pane is back at the shell, then run the same command with
`--resume <session id>` (or the agent's own resume flag).

With update: after the agent has exited, run the agent's self-update command in
the same pane and wait for it, then resume. A failed update does not stop the
resume; the old version is resumed.
"""
import tempfile
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
UPDATE_TIMEOUT = 600
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
# agent kind -> self-update command, run in the pane while the agent is stopped.
# Kinds absent here have no verified update command and are restarted unchanged.
UPDATE = {
    "claude": ["claude", "update"],
    "agy": ["agy", "update"],
    "opencode": ["opencode", "upgrade"],
    "hermes": ["hermes", "update", "--yes"],
}
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


def stop(agent):
    """Ask the agent to exit and wait for the shell. Returns (cmd, error)."""
    pane, kind = agent["pane_id"], agent["agent"]
    argv, _ = launch_argv(pane, kind)
    cmd = build_command(agent, argv)
    if RESUME[kind][1] == "slash":
        herdr("pane", "send-text", pane, "/exit")
        herdr("pane", "send-keys", pane, "enter")
    else:
        herdr("pane", "send-keys", pane, "ctrl+c")
        time.sleep(0.4)
        herdr("pane", "send-keys", pane, "ctrl+c")
    deadline = time.time() + EXIT_TIMEOUT
    while time.time() < deadline:
        if at_shell(pane, kind):
            return cmd, None
        time.sleep(0.5)
    return cmd, f"agent did not exit within {EXIT_TIMEOUT}s"


def run_update(pane, kind):
    """Run the update command in the pane; returns an error text or None."""
    fd, status_file = tempfile.mkstemp(prefix="restart-agents-")
    os.close(fd)
    os.unlink(status_file)
    # sh -c keeps this independent of the pane's shell (fish has no $?).
    script = f"{shlex.join(UPDATE[kind])}; echo $? > {shlex.quote(status_file)}"
    try:
        herdr("pane", "run", pane, shlex.join(["sh", "-c", script]))
        deadline = time.time() + UPDATE_TIMEOUT
        while time.time() < deadline:
            if os.path.exists(status_file):
                time.sleep(0.2)
                code = open(status_file).read().strip()
                return None if code == "0" else f"'{shlex.join(UPDATE[kind])}' exited with {code}"
            time.sleep(0.5)
        return f"update did not finish within {UPDATE_TIMEOUT}s"
    finally:
        if os.path.exists(status_file):
            os.unlink(status_file)


def update_note(kind, err):
    if kind not in UPDATE:
        return f"update not supported for '{kind}'"
    return f"update failed: {err}" if err else "updated"


def resume(agent, cmd, note):
    herdr("pane", "run", agent["pane_id"], shlex.join(cmd))
    detail = shlex.join(cmd)
    return agent["pane_id"], label_of(agent), "restarted", f"{note}; {detail}" if note else detail


def label_of(agent):
    return agent.get("terminal_title_stripped") or agent["pane_id"]


def restart(agent, update=False):
    pane, kind = agent["pane_id"], agent["agent"]
    label = label_of(agent)
    if agent["agent_status"] in SKIP_STATES:
        return pane, label, "skipped", f"agent is {agent['agent_status']}"
    try:
        cmd, err = stop(agent)
        if err:
            return pane, label, "failed", err
        note = None
        if update:
            note = update_note(kind, run_update(pane, kind) if kind in UPDATE else None)
        return resume(agent, cmd, note)
    except Exception as e:  # report per pane, keep going with the others
        return pane, label, "failed", str(e)


def restart_many(agents, update=False):
    if not update:
        with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
            return list(ex.map(restart, agents))
    # With update: stop every pane, update each agent kind once (in the first
    # stopped pane of that kind), then resume all panes.
    def stop_one(agent):
        if agent["agent_status"] in SKIP_STATES:
            return agent, None, f"agent is {agent['agent_status']}", True
        try:
            cmd, err = stop(agent)
            return agent, cmd, err, False
        except Exception as e:
            return agent, None, str(e), False

    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        stopped = list(ex.map(stop_one, agents))
    first_pane, notes = {}, {}
    for agent, cmd, err, _ in stopped:
        if not err:
            first_pane.setdefault(agent["agent"], agent["pane_id"])
    updatable = [k for k in first_pane if k in UPDATE]
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        errs = dict(zip(updatable, ex.map(lambda k: run_update(first_pane[k], k), updatable)))
    for kind in first_pane:
        notes[kind] = update_note(kind, errs.get(kind))

    def resume_one(item):
        agent, cmd, err, skipped = item
        pane, label = agent["pane_id"], label_of(agent)
        if skipped:
            return pane, label, "skipped", err
        if err:
            return pane, label, "failed", err
        try:
            return resume(agent, cmd, notes[agent["agent"]])
        except Exception as e:
            return pane, label, "failed", str(e)

    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        return list(ex.map(resume_one, stopped))


def print_results(results):
    for pane, label, status, detail in results:
        print(f"{status:10} {pane:8} {label}  {detail}")
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


def parse_numbers(text, count):
    """'1 3 5-7' -> {1, 3, 5, 6, 7}; raises ValueError on anything else."""
    out = set()
    for part in text.replace(",", " ").split():
        lo, _, hi = part.partition("-")
        lo, hi = int(lo), int(hi or lo)
        if not 1 <= lo <= hi <= count:
            raise ValueError(part)
        out.update(range(lo, hi + 1))
    return out


def names(kind):
    """id -> label for 'tab' or 'workspace'."""
    try:
        items = herdr(kind, "list")["result"][kind + "s"]
        return {i[kind + "_id"]: i.get("label", "") for i in items}
    except Exception:
        return {}


def current_scope(agents):
    """(tab_id, workspace_id) of the focused agent or of the invoking pane."""
    pane = focused_pane()
    for a in agents:
        if a["pane_id"] == pane:
            return a.get("tab_id"), a.get("workspace_id")
    return None, None


def select_agents(agents, update):
    """Interactive checklist; returns the agents to restart ([] if cancelled)."""
    tabs, workspaces = names("tab"), names("workspace")
    agents = sorted(agents, key=lambda a: (a.get("workspace_id", ""), a.get("tab_id", ""), a["pane_id"]))
    busy = {i for i, a in enumerate(agents) if a["agent_status"] in SKIP_STATES}
    selected = set(range(len(agents))) - busy
    cur_tab, cur_ws = current_scope(agents)
    verb = "Update and restart" if update else "Restart"
    msg = ""
    while True:
        print("\033[2J\033[H", end="")  # redraw the list from the top
        print(f"{verb} agents. [x] = will be restarted. Agents working/blocked are skipped.\n")
        for i, a in enumerate(agents):
            mark = "[-]" if i in busy else "[x]" if i in selected else "[ ]"
            where = f"{workspaces.get(a.get('workspace_id'), '?')} / {tabs.get(a.get('tab_id'), '?')}"
            note = f"  ({a['agent_status']}, skipped)" if i in busy else ""
            print(f"{i + 1:3} {mark} {label_of(a)}  [{a['agent']}]  {where}{note}")
        if update:
            print("\nEach agent kind is updated once, then every selected pane is resumed.")
        print("\nNumbers (1 3 5-7) toggle; a = select all; n = select none;"
              "\nt = only this tab; w = only this workspace; Enter = go; q = cancel")
        if msg:
            print(f"\n{msg}")
            msg = ""
        try:
            cmd = input(f"[{len(selected)} selected] > ").strip().lower()
        except EOFError:
            return []
        if cmd == "q":
            return []
        if cmd == "":
            if selected:
                return [agents[i] for i in sorted(selected)]
            msg = "Nothing selected."
        elif cmd == "a":
            selected = set(range(len(agents))) - busy
        elif cmd == "n":
            selected = set()
        elif cmd in ("t", "w"):
            key, cur = ("tab_id", cur_tab) if cmd == "t" else ("workspace_id", cur_ws)
            if cur is None:
                msg = "Current tab/workspace is unknown."
            else:
                selected = {i for i, a in enumerate(agents) if a.get(key) == cur} - busy
        else:
            try:
                for i in parse_numbers(cmd, len(agents)):
                    if i - 1 in busy:
                        continue
                    selected ^= {i - 1}
            except ValueError:
                msg = f"Not understood: '{cmd}'. Use numbers like 1 3 5-7, or a, n, t, w, q."


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "current"
    update = mode.endswith("-update")
    mode = mode.removesuffix("-update")
    if mode == "current":
        pane = focused_pane()
        agent = next((a for a in list_agents() if a["pane_id"] == pane), None)
        if not agent:
            print("no agent in the focused pane")
            return 1
        print_results([restart(agent, update)])
    elif mode == "open-confirm":
        herdr("plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint",
              "confirm-all-update" if update else "confirm-all")
    elif mode == "all-ui":
        chosen = select_agents(list_agents(), update)
        if chosen:
            print()
            print_results(restart_many(chosen, update))
        input("\nEnter to close")
    return 0


if __name__ == "__main__":
    sys.exit(main())
