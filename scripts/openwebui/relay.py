"""Host TCP relay for the Open WebUI dev environment (#1070, from #1073). Dev only, stdlib.

The maintainer's local agent listens on 127.0.0.1 only (scripts/run_local_stack.sh),
which a Kind pod cannot reach. This relay listens on the Kind network gateway
address and forwards each connection unchanged to the agent:

    python3 -I relay.py --listen 172.18.0.1:18080 --target 127.0.0.1:8080 --allow 172.18.0.2

Only the listed peers (the dev Kind node) may connect; any other peer is
closed at once. There is no idle timeout: an answer may think for minutes with
no bytes on the wire (the Windows console relay lesson, 2026-10-03). Logs carry
counts and peer addresses only, never payload.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys

CHUNK = 65536


def _split(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {value!r}")
    return host, int(port)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except (ConnectionError, OSError, RuntimeError):
            pass


class Relay:
    def __init__(self, target: tuple[str, int], allow: frozenset[str]) -> None:
        self.target = target
        self.allow = allow
        self.accepted = 0
        self.refused = 0
        self.failed = 0

    async def handle(self, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        peer = client_w.get_extra_info("peername") or ("?", 0)
        if peer[0] not in self.allow:
            self.refused += 1
            print(f"refused peer {peer[0]}", flush=True)
            client_w.close()
            return
        try:
            up_r, up_w = await asyncio.open_connection(*self.target)
        except OSError:
            self.failed += 1
            print(f"target {self.target[0]}:{self.target[1]} unreachable (failed={self.failed})", flush=True)
            client_w.close()
            return
        self.accepted += 1
        try:
            await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))
        finally:
            for w in (up_w, client_w):
                w.close()


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listen", type=_split, required=True)
    parser.add_argument("--target", type=_split, required=True)
    parser.add_argument("--allow", action="append", required=True,
                        help="peer IPv4 address allowed to connect (repeatable)")
    args = parser.parse_args(argv)
    relay = Relay(args.target, frozenset(args.allow))
    server = await asyncio.start_server(relay.handle, *args.listen)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    print(f"relay {args.listen[0]}:{args.listen[1]} -> {args.target[0]}:{args.target[1]} "
          f"allow={sorted(relay.allow)}", flush=True)
    async with server:
        await stop.wait()
    print(f"relay stopped accepted={relay.accepted} refused={relay.refused} failed={relay.failed}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
