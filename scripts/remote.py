"""
Remote Server Management
========================
SSH-based job distribution, process monitoring, and cleanup.

Usage as library:
    from remote import RemoteNode
    node = RemoteNode("node007.bosccluster.com")
    node.try_run(cmd, exec_name="gem5.fast", max_procs=64)

Usage as CLI:
    python remote.py -e gem5.fast -s node007 node008 --check
    python remote.py -e gem5.fast -s node007 node008 --kill
    python remote.py -e gem5.fast -s node007 -c "sleep 100 &" --run -n 64
"""

import logging
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

import paramiko

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())   # silent when imported as library


def _silence_paramiko_logs() -> None:
    """Disable Paramiko log output by default for both CLI and import usage."""
    for name in ("paramiko", "paramiko.transport"):
        logger = logging.getLogger(name)
        logger.disabled = True
        logger.propagate = False


_silence_paramiko_logs()


# ═══════════════════════════════════════════════════════════════
#  SSH helper
# ═══════════════════════════════════════════════════════════════

@contextmanager
def _ssh(server: str):
    """Context-managed SSH connection with auto-close."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=server)
    try:
        yield client
    finally:
        client.close()


def _ssh_read(client: paramiko.SSHClient, cmd: str) -> str:
    """Run a command and return stripped stdout."""
    _, stdout, _ = client.exec_command(cmd)
    return stdout.read().decode().strip()


# ═══════════════════════════════════════════════════════════════
#  Node status
# ═══════════════════════════════════════════════════════════════

@dataclass
class NodeStatus:
    server: str
    running: int
    cores: int
    load_1m: float
    load_5m: float
    load_15m: float

    @property
    def load_threshold(self) -> float:
        return self.cores / 2

    @property
    def can_accept(self) -> bool:
        return self.load_1m < self.load_threshold

    def summary(self) -> str:
        if self.running == 0:
            tag = "idle"
        elif not self.can_accept:
            tag = "HIGH LOAD"
        else:
            tag = "ok"
        return (
            f"{self.server:30s}  "
            f"procs={self.running:3d}  "
            f"load={self.load_1m:.1f}/{self.load_5m:.1f}/{self.load_15m:.1f}  "
            f"cores={self.cores}  "
            f"[{tag}]"
        )


def query_status(server: str, exec_name: str) -> NodeStatus:
    """SSH into *server* and return a NodeStatus snapshot."""
    with _ssh(server) as client:
        running = int(_ssh_read(client, f"pgrep -c -f {exec_name} -u $(whoami)") or "0")
        cores = int(_ssh_read(client, "nproc"))
        uptime = _ssh_read(client, "uptime")
        loads = uptime.split("load average: ")[1].split(", ")
        l1, l5, l15 = float(loads[0]), float(loads[1]), float(loads[2])

    return NodeStatus(server=server, running=running, cores=cores,
                      load_1m=l1, load_5m=l5, load_15m=l15)


# ═══════════════════════════════════════════════════════════════
#  Core operations
# ═══════════════════════════════════════════════════════════════

def check_load_and_run(server: str, cmd: str, exec_name: str,
                       max_procs: int) -> bool:
    """
    If *server* has capacity, fire *cmd* over SSH and return True.
    Otherwise return False without running anything.

    Backward-compatible API used by run.py.
    """
    try:
        status = query_status(server, exec_name)
        if status.running >= max_procs or not status.can_accept:
            log.debug("skip %s: procs=%d load=%.1f",
                      server, status.running, status.load_1m)
            return False

        with _ssh(server) as client:
            client.exec_command(cmd)

        log.debug("dispatched to %s", server)
        return True

    except Exception as e:
        log.warning("failed on %s: %s", server, e)
        return False


def kill_all(server: str, exec_name: str) -> int:
    """Kill all user-owned *exec_name* processes on *server*. Returns kill count."""
    try:
        with _ssh(server) as client:
            before = int(_ssh_read(client, f"pgrep -c -f {exec_name} -u $(whoami)") or "0")
            killed = int(_ssh_read(client, f"pkill -c -f {exec_name} -u $(whoami)") or "0")
            remain = before - killed

        log.info("%s: killed=%d remain=%d", server, killed, remain)
        return killed

    except Exception as e:
        log.warning("failed on %s: %s", server, e)
        return 0


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="Remote server management")
    p.add_argument("-e", "--exec", required=True, dest="exec_name",
                   help="Process name to match (e.g. gem5.fast)")
    p.add_argument("-s", "--server", nargs="+", default=["localhost"],
                   help="Server hostnames")

    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true",
                       help="Show process status and load")
    group.add_argument("--kill", action="store_true",
                       help="Kill all matching processes")
    group.add_argument("--run", action="store_true",
                       help="Distribute a command")

    p.add_argument("-c", "--cmd", nargs="+", default=[""],
                   help="Command to run (with --run)")
    p.add_argument("-n", "--num", type=int, default=64,
                   help="Max processes per server (with --run)")
    p.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.check:
        for server in args.server:
            try:
                s = query_status(server, args.exec_name)
                log.info(s.summary())
            except Exception as e:
                log.error("%s: %s", server, e)

    elif args.kill:
        for server in args.server:
            kill_all(server, args.exec_name)

    elif args.run:
        cmd_str = " ".join(args.cmd)
        log.info("cmd: %s", cmd_str)
        for server in args.server:
            ok = check_load_and_run(server, cmd_str, args.exec_name, args.num)
            if ok:
                log.info("dispatched to %s", server)


if __name__ == "__main__":
    main()