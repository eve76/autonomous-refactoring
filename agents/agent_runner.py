"""Subprocess runner for analyst/programmer agents via the Claude CLI.

Replaces the original KIRO CLI integration. Each agent runs as an
independent `claude` subprocess with the system prompt supplied via
flag and the task prompt streamed in on stdin. Standard output is
streamed line-by-line to a per-agent log file for debugging.
"""

import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, IO


@dataclass
class AgentProcess:
    agent_id: str
    process: subprocess.Popen
    log_file: IO
    stdout_reader: threading.Thread
    last_log_line: str = ""
    edits_made: bool = False
    gate_invocations: int = 0
    metadata: dict = field(default_factory=dict)

    def is_running(self) -> bool:
        return self.process.poll() is None

    def kill(self) -> None:
        try:
            self.process.kill()
        except ProcessLookupError:
            pass
        try:
            self.log_file.close()
        except Exception:
            pass


def spawn_claude_agent(
    agent_id: str,
    cli_path: str,
    cwd: Path,
    system_prompt: str,
    task_prompt: str,
    log_path: Path,
    model: str,
    extra_args: Optional[list[str]] = None,
) -> AgentProcess:
    """Launch one `claude` CLI process and wire up a log streamer."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", buffering=1)

    cmd = [
        cli_path,
        "-p",
        "--append-system-prompt", system_prompt,
        "--permission-mode", "acceptEdits",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
    ]
    if extra_args:
        cmd.extend(extra_args)

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    if proc.stdin is not None:
        proc.stdin.write(task_prompt)
        proc.stdin.close()

    agent = AgentProcess(
        agent_id=agent_id,
        process=proc,
        log_file=log_file,
        stdout_reader=None,  # type: ignore[arg-type]
    )

    reader = threading.Thread(
        target=_stream_stdout,
        args=(agent,),
        daemon=True,
    )
    agent.stdout_reader = reader
    reader.start()

    return agent


_EDIT_TOOL_MARKERS = (
    '"name":"Edit"',
    '"name":"Write"',
    '"name":"MultiEdit"',
    '"name": "Edit"',
    '"name": "Write"',
    '"name": "MultiEdit"',
)


def _stream_stdout(agent: AgentProcess) -> None:
    """Stream subprocess stdout to disk and pick up two activity signals.

    These are heuristic substring matches over raw stream-json lines so
    the orchestrator's stuck-agent evaluation has something to look at.
    """
    assert agent.process.stdout is not None
    try:
        for line in agent.process.stdout:
            agent.log_file.write(line)
            agent.last_log_line = line.rstrip("\n")
            if any(m in line for m in _EDIT_TOOL_MARKERS):
                agent.edits_made = True
            if "merge_gate/cli.py" in line and '"name":"Bash"' in line.replace(" ", ""):
                agent.gate_invocations += 1
    finally:
        try:
            agent.log_file.flush()
            agent.log_file.close()
        except Exception:
            pass
