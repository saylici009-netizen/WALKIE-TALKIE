#!/usr/bin/env python3
"""
Talkie-Walkie PTT Server (UDP Relay)
=====================================
Lightweight UDP relay server. Receives audio datagrams from any client
and broadcasts them to every other connected client.

Clients register automatically when they send their first packet.
Stale clients are pruned after 30 seconds of silence.

Usage:
    python server.py [--port PORT]
"""

import socket
import argparse
import time
import threading
import logging

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ptt-server")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BUFFER_SIZE = 8192          # max UDP datagram we'll accept
CLIENT_TIMEOUT = 30.0       # seconds before a silent client is pruned
PRUNE_INTERVAL = 10.0       # how often to run the pruner


def main() -> None:
    parser = argparse.ArgumentParser(description="Talkie-Walkie UDP Relay Server")
    parser.add_argument("--port", type=int, default=9000, help="Listen port (default: 9000)")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))

    # addr → last_seen timestamp
    clients: dict[tuple, float] = {}
    clients_lock = threading.Lock()

    log.info("═" * 52)
    log.info("  🎙  Talkie-Walkie PTT Server  (UDP)")
    log.info("  Listening on 0.0.0.0:%d", args.port)
    log.info("═" * 52)

    # ── Stale client pruner ──────────────────────────────────────────
    def prune_loop():
        while True:
            time.sleep(PRUNE_INTERVAL)
            now = time.monotonic()
            with clients_lock:
                stale = [a for a, t in clients.items() if now - t > CLIENT_TIMEOUT]
                for a in stale:
                    del clients[a]
                    log.info("⏏  Pruned stale client %s:%d", *a)

    threading.Thread(target=prune_loop, daemon=True).start()

    # ── Main relay loop ──────────────────────────────────────────────
    try:
        while True:
            data, addr = sock.recvfrom(BUFFER_SIZE)

            # Register / refresh client
            is_new = False
            with clients_lock:
                if addr not in clients:
                    is_new = True
                clients[addr] = time.monotonic()

            if is_new:
                log.info("✚  Client joined: %s:%d  (total: %d)", *addr, len(clients))

            # Skip control packets (like PING)
            if len(data) < 32:
                continue

            # Broadcast to everyone else
            with clients_lock:
                targets = [a for a in clients if a != addr]
            for target in targets:
                try:
                    sock.sendto(data, target)
                except OSError:
                    pass

    except KeyboardInterrupt:
        log.info("\nShutting down server…")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
