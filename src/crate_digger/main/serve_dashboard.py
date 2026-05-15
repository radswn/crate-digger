import argparse
import os
import signal
import time
from pathlib import Path

import uvicorn

from crate_digger.web.app import create_app

LISTEN_STATE = "0A"
SOCKET_PREFIX = "socket:["


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local collection dashboard.")
    parser.add_argument("--config", default="config.toml", help="Path to config TOML.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", default=8765, type=int, help="Port to bind.")
    parser.add_argument(
        "--restart-existing",
        action="store_true",
        help="Terminate any existing process listening on the selected port first.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.restart_existing:
        stop_existing_listeners(args.port)

    app = create_app(args.config)
    uvicorn.run(app, host=args.host, port=args.port)


def stop_existing_listeners(port: int) -> None:
    pids = _pids_listening_on_port(port)
    current_pid = os.getpid()
    pids.discard(current_pid)

    if not pids:
        return

    for pid in sorted(pids):
        print(f"Stopping existing dashboard listener on port {port} (pid {pid})")
        os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _pids_listening_on_port(port).difference({current_pid}):
            return
        time.sleep(0.1)

    still_running = sorted(_pids_listening_on_port(port).difference({current_pid}))
    if still_running:
        raise RuntimeError(
            f"Port {port} is still in use after SIGTERM by pid(s): {still_running}"
        )


def _pids_listening_on_port(port: int) -> set[int]:
    socket_inodes = _listening_socket_inodes(port)
    if not socket_inodes:
        return set()

    pids: set[int] = set()
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue

        fd_dir = proc_dir / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if _socket_inode_from_link(target) in socket_inodes:
                    pids.add(int(proc_dir.name))
                    break
        except OSError:
            continue

    return pids


def _listening_socket_inodes(port: int) -> set[str]:
    inodes: set[str] = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = table.read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue

        for line in lines:
            fields = line.split()
            if len(fields) < 10:
                continue

            local_address = fields[1]
            state = fields[3]
            inode = fields[9]
            if state != LISTEN_STATE:
                continue

            _, raw_port = local_address.rsplit(":", maxsplit=1)
            if int(raw_port, 16) == port:
                inodes.add(inode)

    return inodes


def _socket_inode_from_link(target: str) -> str | None:
    if not target.startswith(SOCKET_PREFIX) or not target.endswith("]"):
        return None
    return target.removeprefix(SOCKET_PREFIX).removesuffix("]")


if __name__ == "__main__":
    main()
