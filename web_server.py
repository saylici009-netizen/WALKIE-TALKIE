#!/usr/bin/env python3
"""
Talkie-Walkie Web Server (Flask-SocketIO)
==========================================
Production-grade Push-to-Talk radio server bridging mobile browsers
and desktop UDP clients with real-time audio streaming.

Architecture:
    Mobile Phone ──WebSocket──► web_server.py ──UDP──► server.py ──UDP──► client.py (laptop speaker)
    Mobile Phone ◄──WebSocket── web_server.py ◄──UDP── server.py ◄──UDP── client.py (laptop mic)

Features:
    • WebSocket (Socket.IO) real-time audio relay
    • UDP bridge to desktop relay server
    • Auto-reconnect UDP keepalive
    • Per-client statistics and session tracking
    • Connection health monitoring
    • Thread-safe client registry with stale pruning

Usage:
    pip install flask flask-socketio
    python web_server.py [--port 5000] [--udp-host 127.0.0.1] [--udp-port 9000]
"""

import os
import sys
import time
import struct
import socket
import logging
import argparse
import threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

# ═══════════════════════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ptt-web")

# ═══════════════════════════════════════════════════════════════════════════
# Flask + Socket.IO Application
# ═══════════════════════════════════════════════════════════════════════════
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    max_http_buffer_size=1_000_000,  # 1 MB max per message (audio chunks are ~2 KB)
    ping_timeout=30,
    ping_interval=10,
)

# ═══════════════════════════════════════════════════════════════════════════
# Client Registry (thread-safe)
# ═══════════════════════════════════════════════════════════════════════════
clients = {}          # sid → {addr, joined_at, name, tx_chunks, rx_chunks, last_tx}
clients_lock = threading.Lock()

# Statistics
stats = {
    "total_connections": 0,
    "total_audio_chunks_relayed": 0,
    "total_bytes_relayed": 0,
    "server_start": time.time(),
}

# ═══════════════════════════════════════════════════════════════════════════
# UDP Bridge (connects web server ↔ desktop UDP relay server)
# ═══════════════════════════════════════════════════════════════════════════
UDP_SERVER_ADDR = ("127.0.0.1", 9000)  # updated by CLI args
udp_sock = None
udp_bridge_active = False


def init_udp_bridge(udp_host: str, udp_port: int):
    """Initialize the UDP bridge socket and start background threads."""
    global udp_sock, UDP_SERVER_ADDR, udp_bridge_active

    UDP_SERVER_ADDR = (udp_host, udp_port)
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.settimeout(2.0)  # non-blocking-ish for clean shutdown
    udp_sock.bind(("0.0.0.0", 0))  # random ephemeral port

    local_port = udp_sock.getsockname()[1]
    log.info("  UDP bridge bound to 0.0.0.0:%d → relay %s:%d", local_port, udp_host, udp_port)

    # Register with the UDP relay
    try:
        udp_sock.sendto(b"PING", UDP_SERVER_ADDR)
        udp_bridge_active = True
        log.info("  UDP bridge registered with relay server")
    except OSError as e:
        log.warning("  UDP relay unreachable: %s (will retry)", e)

    # Start background threads
    threading.Thread(target=_udp_receive_loop, daemon=True, name="udp-rx").start()
    threading.Thread(target=_udp_keepalive_loop, daemon=True, name="udp-ping").start()


def _udp_receive_loop():
    """Receive audio from Desktop UDP clients and forward to Web clients."""
    global udp_bridge_active
    while True:
        try:
            data, addr = udp_sock.recvfrom(8192)
            if len(data) >= 32:
                # Forward to all connected web clients
                socketio.emit("audio", data)
                stats["total_audio_chunks_relayed"] += 1
                stats["total_bytes_relayed"] += len(data)
        except socket.timeout:
            continue
        except OSError as e:
            log.error("UDP receive error: %s", e)
            udp_bridge_active = False
            time.sleep(2)


def _udp_keepalive_loop():
    """Keep the UDP registration alive with periodic PINGs."""
    global udp_bridge_active
    while True:
        try:
            udp_sock.sendto(b"PING", UDP_SERVER_ADDR)
            if not udp_bridge_active:
                log.info("  UDP bridge reconnected to relay server")
                udp_bridge_active = True
        except OSError:
            if udp_bridge_active:
                log.warning("  UDP relay lost — retrying every 10s")
                udp_bridge_active = False
        time.sleep(10)


# ═══════════════════════════════════════════════════════════════════════════
# HTTP Routes
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    """Serve the PTT radio web UI."""
    return render_template("index.html")


@app.route("/status")
def status():
    """Health / debug endpoint — shows connected clients and stats."""
    with clients_lock:
        client_list = [
            {
                "sid": sid[:8],
                "addr": info["addr"],
                "name": info["name"],
                "connected_at": datetime.fromtimestamp(info["joined_at"]).isoformat(),
                "tx_chunks": info["tx_chunks"],
                "rx_chunks": info["rx_chunks"],
            }
            for sid, info in clients.items()
        ]

    uptime = time.time() - stats["server_start"]
    return jsonify({
        "status": "ok",
        "uptime_seconds": round(uptime, 1),
        "web_clients": len(client_list),
        "udp_bridge": udp_bridge_active,
        "total_chunks_relayed": stats["total_audio_chunks_relayed"],
        "total_bytes_relayed": stats["total_bytes_relayed"],
        "clients": client_list,
    })


# ═══════════════════════════════════════════════════════════════════════════
# Socket.IO Event Handlers
# ═══════════════════════════════════════════════════════════════════════════
@socketio.on("connect")
def handle_connect():
    sid = request.sid
    addr = request.remote_addr

    with clients_lock:
        clients[sid] = {
            "addr": addr,
            "joined_at": time.time(),
            "name": f"User-{sid[:6]}",
            "tx_chunks": 0,
            "rx_chunks": 0,
            "last_tx": 0,
        }
        count = len(clients)

    stats["total_connections"] += 1
    log.info("✚  Web client connected: %s from %s  (total: %d)", sid[:8], addr, count)
    emit("status", {"type": "connected", "users": count}, broadcast=True)


@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    with clients_lock:
        info = clients.pop(sid, None)
        count = len(clients)

    name = info["name"] if info else "unknown"
    log.info("⏏  Web client disconnected: %s (%s)  (total: %d)", sid[:8], name, count)
    emit("status", {"type": "disconnected", "users": count}, broadcast=True)


@socketio.on("audio")
def handle_audio(data):
    """
    Receive an audio chunk from a web client.
    • Broadcast to all OTHER web clients
    • Forward to desktop clients via UDP relay server
    """
    sid = request.sid

    # Update sender stats
    with clients_lock:
        if sid in clients:
            clients[sid]["tx_chunks"] += 1
            clients[sid]["last_tx"] = time.time()

    # Broadcast to every other web client
    emit("audio", data, broadcast=True, include_self=False)

    # Forward to desktop UDP relay
    if udp_bridge_active and udp_sock:
        try:
            udp_sock.sendto(data, UDP_SERVER_ADDR)
        except OSError:
            pass

    stats["total_audio_chunks_relayed"] += 1
    stats["total_bytes_relayed"] += len(data) if isinstance(data, (bytes, bytearray)) else 0


@socketio.on("set_name")
def handle_set_name(data):
    sid = request.sid
    with clients_lock:
        if sid in clients:
            old_name = clients[sid]["name"]
            clients[sid]["name"] = data.get("name", f"User-{sid[:6]}")
            log.info("📛  %s → %s", old_name, clients[sid]["name"])


# ═══════════════════════════════════════════════════════════════════════════
# Network Helpers
# ═══════════════════════════════════════════════════════════════════════════
def get_lan_ip() -> str:
    """Best-effort detection of this machine's LAN IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ═══════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Talkie-Walkie Web PTT Server")
    parser.add_argument("--port", type=int, default=5000, help="Web server port (default: 5000)")
    parser.add_argument("--udp-host", type=str, default="127.0.0.1", help="UDP relay host (default: 127.0.0.1)")
    parser.add_argument("--udp-port", type=int, default=9000, help="UDP relay port (default: 9000)")
    args = parser.parse_args()

    lan_ip = get_lan_ip()

    log.info("═" * 56)
    log.info("  🎙  Talkie-Walkie Web PTT Server")
    log.info("─" * 56)
    log.info("  Web UI:   http://0.0.0.0:%d", args.port)
    log.info("  LAN URL:  http://%s:%d", lan_ip, args.port)
    log.info("  UDP relay: %s:%d", args.udp_host, args.udp_port)
    log.info("  Status:   http://%s:%d/status", lan_ip, args.port)
    log.info("═" * 56)
    log.info("")
    log.info("  📱  Open the LAN URL on your phone to start talking!")
    log.info("")

    # Initialize UDP bridge
    init_udp_bridge(args.udp_host, args.udp_port)

    # Run Flask-SocketIO server
    socketio.run(
        app,
        host="0.0.0.0",
        port=args.port,
        debug=False,
        use_reloader=False,
        log_output=True,
    )
