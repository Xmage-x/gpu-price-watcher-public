#!/usr/bin/env python3
"""Tiny TCP forwarder used to bridge a container port onto a host bind address.

Pair it with the noVNC setup scripts to view browser sessions from other
machines on your own network. Keep the listen address private (loopback or LAN);
do not expose it on a public interface.
"""

from __future__ import annotations

import argparse
import selectors
import socket
import threading


def pipe(left: socket.socket, right: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    sockets = [left, right]
    try:
        while True:
            for key, _ in selector.select():
                src = key.fileobj
                dst = key.data
                data = src.recv(65536)
                if not data:
                    return
                dst.sendall(data)
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Forward TCP connections.")
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, required=True)
    args = parser.parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.listen_host, args.listen_port))
    server.listen(50)
    print(
        f"forwarding {args.listen_host}:{args.listen_port} -> {args.target_host}:{args.target_port}",
        flush=True,
    )
    try:
        while True:
            client, _ = server.accept()
            target = socket.create_connection((args.target_host, args.target_port), timeout=10)
            thread = threading.Thread(target=pipe, args=(client, target), daemon=True)
            thread.start()
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
