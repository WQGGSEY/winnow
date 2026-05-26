"""Thread supervisor — true hands-free e2e automation.

Watches a single thread, spawns `claude` CLI subprocesses via pty when the
MCP server has been idle past a threshold, feeds the resume prompt
deterministically, and only terminates when the thread reaches a terminal
outcome (paper accept or honest_failure).

This is the missing piece between the MCP server (which exposes tools but
needs Claude Code interactive to drive them) and operator hands-free
operation. The operator runs:

    python -m research_harness.thread_supervisor watch <thread_id>

once. The supervisor handles every subsequent session respawn until the
thread terminates. No API tokens used — the spawned `claude` subprocess
runs under the operator's subscription pool.

Design constraints:
- Subscription pool only. No Anthropic API path, ever.
- pty stdlib only — zero new dependencies.
- Single supervisor per thread (lock file enforced).
- Survives subprocess crashes, claude CLI session limits, MCP daemon
  restarts. The only fatal state is operator SIGINT or thread terminal.
"""

from __future__ import annotations

import argparse
import atexit
import errno
import json
import os
import pty
import select
import signal
import sys
import time
from pathlib import Path

LOG = sys.stderr


# --- thresholds (configurable via CLI flags) ----------------------------- #

DEFAULT_MAX_IDLE_SECONDS = 600.0   # 10 min — claude session typically idle
                                   #          between MCP calls during heavy
                                   #          experiments; we want to detect
                                   #          true death, not a long step.
DEFAULT_POLL_SECONDS = 30.0        # check every 30s
DEFAULT_MAX_CYCLES = 12            # hard cap on respawn cycles per thread
DEFAULT_CLAUDE_BOOT_DELAY = 3.0    # seconds to wait after fork before
                                   #          writing the resume prompt
DEFAULT_CLAUDE_MODEL = "claude-opus-4-7"


TERMINAL_OUTCOMES = {
    "accept",        # render_final_paper finished
    "honest_failure",  # render_honest_failure_paper finished
}


# --- terminal-state detection ------------------------------------------- #


def _thread_dir(repo: Path, tid: str) -> Path:
    return repo / "runs" / "threads" / tid


def is_terminal(repo: Path, tid: str) -> tuple[bool, str | None]:
    """Return (is_terminal, outcome). Terminal means the thread has emitted
    a publication artifact and a production_run_summary.json with a
    recognized outcome — render_final_paper or render_honest_failure_paper.
    """
    summary_path = _thread_dir(repo, tid) / "production" / "production_run_summary.json"
    if not summary_path.exists():
        return False, None
    try:
        s = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, None
    # honest_failure path writes outcome at top level.
    direct = s.get("outcome")
    if direct in TERMINAL_OUTCOMES:
        return True, direct
    # accept path: render_final_paper writes rebuttal_summary.ac_decision.decision.
    ac_decision = (s.get("rebuttal_summary") or {}).get("ac_decision", {}).get("decision")
    if ac_decision in {"accept", "revise"} and (
        s.get("publication_dispatch", {}) or {}
    ).get("rendered_artifacts"):
        return True, "accept"
    return False, None


# --- MCP idle detection ------------------------------------------------- #


def mcp_idle_seconds(repo: Path, tid: str) -> float:
    """Return seconds since the most recent file mtime under the thread's
    production directory. Used as a proxy for MCP activity — any
    handle_submit_*, handle_render_*, or handle_revise_root_after_reject
    call writes at least one file.

    Returns inf when the production dir is empty or missing.
    """
    pdir = _thread_dir(repo, tid) / "production"
    if not pdir.exists():
        return float("inf")
    most_recent = 0.0
    for sub in pdir.rglob("*"):
        if sub.is_file():
            try:
                most_recent = max(most_recent, sub.stat().st_mtime)
            except OSError:
                continue
    if most_recent == 0.0:
        return float("inf")
    return max(0.0, time.time() - most_recent)


# --- resume prompt assembly --------------------------------------------- #


def _last_known_state_summary(repo: Path, tid: str) -> dict[str, object]:
    """Pull a few cheap facts from the thread state for the resume prompt.
    Best-effort — missing files return defaults."""
    pdir = _thread_dir(repo, tid) / "production"
    summary: dict[str, object] = {"thread_id": tid}
    state_path = pdir / "tree" / "search_state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            summary["promoted_node_ids"] = state.get("promoted_node_ids") or []
            summary["pruned_node_ids"] = state.get("pruned_node_ids") or []
            ready_or_running = [
                n["id"] for n in state.get("nodes", [])
                if n.get("status") in {
                    "ready", "running", "completed_worker_report",
                    "critic_reviewed", "orchestrator_reduced",
                }
            ]
            summary["mid_state_nodes"] = ready_or_running
            summary["search_state_status"] = state.get("status")
        except (OSError, json.JSONDecodeError):
            pass
    ac_path = pdir / "rebuttal" / "ac_decision.json"
    if ac_path.exists():
        try:
            ac = json.loads(ac_path.read_text(encoding="utf-8"))
            summary["last_ac_decision"] = ac.get("decision")
        except (OSError, json.JSONDecodeError):
            pass
    archived = sorted(p.name for p in _thread_dir(repo, tid).glob("production.attempt_*"))
    summary["archived_attempts"] = len(archived)
    return summary


def build_resume_prompt(repo: Path, tid: str, cycle: int) -> str:
    state = _last_known_state_summary(repo, tid)
    mid = state.get("mid_state_nodes") or []
    promoted = state.get("promoted_node_ids") or []
    archived = state.get("archived_attempts", 0)
    last_ac = state.get("last_ac_decision")

    lines = [
        f"research_harness MCP에 붙어있어. Thread {tid} 를 이어서 진행할 거야.",
        "",
        f"[supervisor cycle #{cycle}] 이전 세션이 한도로 종료됐어. 너가 새 세션이고,",
        "이전 세션이 남긴 thread 디스크 상태를 읽어서 어디서 멈췄는지 파악해야 해.",
        "",
        "현재 상태 (디스크 스냅샷):",
        f"  - promoted nodes: {promoted}",
        f"  - mid-state nodes (ready/running/completed_worker_report/...): {mid}",
        f"  - archived previous attempts: {archived}",
        f"  - last AC decision: {last_ac!r}",
        f"  - search state status: {state.get('search_state_status')!r}",
        "",
        "방향:",
        "  1. get_research_state(thread_id=\"" + tid + "\") 호출로 정확한 현재 상태 확인.",
        "  2. mid-state 노드가 있으면 그 노드의 다음 도구 (resume_production_state",
        "     또는 get_next_admissible_node의 status=resume 응답 따라가기) 호출.",
        "  3. mid-state 노드가 없고 promoted_node_ids도 비어있으면",
        "     get_next_admissible_node로 다음 ready 노드 처리.",
        "  4. publish 단계 도달하면 dual-gate 둘 다 통과해야 render_final_paper 가능:",
        "       (a) submit_ac_decision (decision ∈ {accept, revise}),",
        "       (b) submit_professor_user_goal_attestation (achieved=true).",
        "  5. 방향이 hopeless로 판단되면 propose_alternative_root_directions로",
        "     N≥3 angles fan-out. 끝까지 안 풀리면 render_honest_failure_paper.",
        "",
        "Anti-laziness 룰 작동 중 (PR1):",
        "  - claim narrowing-without-breadth → reject",
        "  - self-made baseline (paper citation 없음) → reject",
        "  - synthetic data without bridging argument → reject",
        "  - disclaimer-only camera_ready_directives (revise일 때) → reject",
        "  - capability claim without decision_rule → reject",
        "Anti-laziness 통과 못 하면 retry 메시지가 explicit reason과 함께 와.",
        "",
        "Memory (PR4):",
        "  - prior failures + active lessons는 prepare_rebuttal_packet /",
        "    prepare_paper_writing_context의 응답 payload에 inject됨.",
        "  - 너의 prune/contradicted 결정은 memory/failures/ 에 자동 기록됨.",
        "",
        "세션 한도 가까워지면 self-judge로 멈춰. 한 줄 status 남기고 종료해.",
        "supervisor가 잠시 후 새 세션 spawn 해서 이어받을 거야.",
        "",
        "지금 시작:",
    ]
    return "\n".join(lines)


# --- pty subprocess spawn ----------------------------------------------- #


def _which(name: str) -> str | None:
    for p in os.environ.get("PATH", "").split(os.pathsep):
        full = os.path.join(p, name)
        if os.access(full, os.X_OK):
            return full
    return None


def spawn_claude_session(
    prompt: str,
    *,
    model: str = DEFAULT_CLAUDE_MODEL,
    boot_delay: float = DEFAULT_CLAUDE_BOOT_DELAY,
    log_path: Path | None = None,
) -> int:
    """Fork+exec `claude --model <model>` under a pty. Write the resume
    prompt after a brief boot delay. Stream child output to log_path
    while also passing through to our stderr so the operator can tail it.

    Returns the child's exit status (WEXITSTATUS).

    Subscription pool only. No API path.
    """
    claude_bin = _which("claude")
    if not claude_bin:
        raise RuntimeError(
            "`claude` CLI not found in PATH. Install Claude Code "
            "and ensure `claude` is on the operator's PATH. "
            "Supervisor uses the subscription pool — no API fallback."
        )

    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: replace with `claude --model <model>`. Inherits OAuth/
        # subscription credentials from the parent shell environment.
        os.execvp(claude_bin, [claude_bin, "--model", model])

    log_fh = log_path.open("ab") if log_path else None
    try:
        # Let claude initialize the TTY surface before we write.
        time.sleep(boot_delay)
        try:
            os.write(master_fd, (prompt + "\n").encode("utf-8"))
        except OSError as exc:
            print(f"[supervisor] failed to write prompt to claude pty: {exc}", file=LOG)
            os.kill(pid, signal.SIGTERM)
            _, status = os.waitpid(pid, 0)
            return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1

        # Drain child output until exit. Block for a chunk, then check
        # liveness; loop until waitpid succeeds.
        buf = bytearray()
        while True:
            try:
                ready, _, _ = select.select([master_fd], [], [], 1.0)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                break
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        break
                    raise
                if not chunk:
                    break
                buf.extend(chunk)
                if log_fh:
                    log_fh.write(chunk)
                    log_fh.flush()
                # Mirror to operator stderr so tail -f works.
                try:
                    LOG.write(chunk.decode("utf-8", errors="replace"))
                    LOG.flush()
                except Exception:  # noqa: BLE001
                    pass
            # Reap child if it has exited.
            try:
                done_pid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if done_pid:
                return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1

        # Fall-through: drain finished without explicit reap. Wait
        # for child to ensure deterministic exit code.
        _, status = os.waitpid(pid, 0)
        return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
    finally:
        if log_fh:
            log_fh.close()
        try:
            os.close(master_fd)
        except OSError:
            pass


# --- supervisor loop ---------------------------------------------------- #


class SupervisorLock:
    """Single-supervisor-per-thread enforcement. Lock file holds the
    supervisor PID; stale locks (PID dead) are auto-cleared."""

    def __init__(self, thread_dir: Path):
        self.path = thread_dir / ".supervisor.lock"

    def acquire(self) -> None:
        if self.path.exists():
            try:
                pid = int(self.path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pid = -1
            if pid > 0 and _pid_alive(pid):
                raise RuntimeError(
                    f"another supervisor (pid={pid}) is already running for "
                    f"this thread (lock: {self.path}). kill it first or wait."
                )
            # stale lock — clear it.
            try:
                self.path.unlink()
            except OSError:
                pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(self.release)

    def release(self) -> None:
        try:
            if self.path.exists() and self.path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else — treat as alive.


def _log(supervisor_log_path: Path, msg: str) -> None:
    """Append a timestamped line to the supervisor log AND mirror to stderr."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        supervisor_log_path.parent.mkdir(parents=True, exist_ok=True)
        with supervisor_log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    print(line, end="", file=LOG, flush=True)


def watch_thread(
    repo: Path,
    tid: str,
    *,
    max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    max_cycles: int = DEFAULT_MAX_CYCLES,
    model: str = DEFAULT_CLAUDE_MODEL,
    boot_delay: float = DEFAULT_CLAUDE_BOOT_DELAY,
) -> dict[str, object]:
    """Main supervisor loop. Returns a final-status dict for the operator.

    Exit conditions:
      - thread reaches terminal outcome (publish or honest_failure) → success
      - max_cycles respawns exhausted → exits with max_cycles_exceeded
      - SIGINT/SIGTERM → graceful (lock released, status='interrupted')
    """
    tdir = _thread_dir(repo, tid)
    if not tdir.exists():
        raise RuntimeError(f"thread directory not found: {tdir}")
    log_path = tdir / "supervisor.log"
    lock = SupervisorLock(tdir)
    lock.acquire()

    interrupted = {"flag": False}
    def _sigint(_signum, _frame):
        interrupted["flag"] = True
        _log(log_path, "SIGINT received — finishing current cycle then exiting.")
    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    _log(log_path, f"supervisor starting for thread {tid} (pid={os.getpid()})")
    _log(log_path, f"max_idle={max_idle_seconds}s poll={poll_seconds}s max_cycles={max_cycles} model={model}")

    cycle = 0
    while True:
        terminal, outcome = is_terminal(repo, tid)
        if terminal:
            _log(log_path, f"thread reached terminal outcome={outcome!r} after {cycle} cycle(s). exiting cleanly.")
            return {"status": "terminal", "outcome": outcome, "cycles": cycle}

        if interrupted["flag"]:
            _log(log_path, "interrupted by signal — exiting.")
            return {"status": "interrupted", "cycles": cycle}

        if cycle >= max_cycles:
            _log(log_path, f"max_cycles ({max_cycles}) exhausted. exiting without terminal outcome.")
            return {"status": "max_cycles_exceeded", "cycles": cycle}

        idle = mcp_idle_seconds(repo, tid)
        if idle <= max_idle_seconds:
            # Recent activity — MCP still being driven by someone. Wait.
            time.sleep(poll_seconds)
            continue

        # Idle past threshold — claude session is gone or never existed.
        # Spawn a new one with the resume prompt.
        cycle += 1
        prompt = build_resume_prompt(repo, tid, cycle)
        _log(log_path, f"cycle #{cycle}: idle={idle:.0f}s > {max_idle_seconds:.0f}s — spawning claude")
        try:
            exit_code = spawn_claude_session(
                prompt,
                model=model,
                boot_delay=boot_delay,
                log_path=tdir / "claude_subprocess.log",
            )
            _log(log_path, f"cycle #{cycle}: claude subprocess exited with code={exit_code}")
        except Exception as exc:  # noqa: BLE001
            _log(log_path, f"cycle #{cycle}: spawn raised {type(exc).__name__}: {exc}")
            # Brief backoff before retrying so we don't spin on a fast-failing
            # CLI (e.g., missing auth, missing binary).
            time.sleep(min(60.0, poll_seconds * 2))


# --- CLI ----------------------------------------------------------------- #


def _repo_root_default() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="research_harness.thread_supervisor",
        description=(
            "True hands-free thread executor: spawn `claude` CLI subprocesses "
            "via pty under subscription pool until the thread reaches a "
            "terminal publication outcome."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_watch = sub.add_parser("watch", help="watch a thread to terminal outcome")
    p_watch.add_argument("thread_id", help="thread id (under runs/threads/)")
    p_watch.add_argument("--repo-root", type=Path, default=None)
    p_watch.add_argument("--max-idle-seconds", type=float, default=DEFAULT_MAX_IDLE_SECONDS)
    p_watch.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    p_watch.add_argument("--max-cycles", type=int, default=DEFAULT_MAX_CYCLES)
    p_watch.add_argument("--model", default=DEFAULT_CLAUDE_MODEL)
    p_watch.add_argument("--boot-delay", type=float, default=DEFAULT_CLAUDE_BOOT_DELAY)

    args = parser.parse_args(argv)
    if args.cmd == "watch":
        repo = (args.repo_root or _repo_root_default()).resolve()
        result = watch_thread(
            repo,
            args.thread_id,
            max_idle_seconds=args.max_idle_seconds,
            poll_seconds=args.poll_seconds,
            max_cycles=args.max_cycles,
            model=args.model,
            boot_delay=args.boot_delay,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("status") in {"terminal", "interrupted"} else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
