#!/usr/bin/env python3
"""
Talkie-Walkie PTT Client
=========================
Professional Push-to-Talk radio client with:
  • UDP audio streaming (lowest latency)
  • ALSA/PulseAudio segfault prevention for Fedora Linux
  • Dark high-tech UI with circular TALK button, level meter, status glow
  • Spacebar global hotkey

Usage:
    python client.py
"""

import os
import sys
import struct
import socket
import threading
import time
import math
import tkinter as tk
from tkinter import font as tkfont

# ═══════════════════════════════════════════════════════════════════════════
# FEDORA LINUX — ALSA / JACK SEGFAULT PREVENTION
# ═══════════════════════════════════════════════════════════════════════════
# Three-layer defence:
#   1. Environment variables to mute ALSA and JACK logging
#   2. ctypes error handler pinned at module scope (prevents GC → segfault)
#   3. stderr redirect during PyAudio import (catches remaining C-level noise)
# ═══════════════════════════════════════════════════════════════════════════

# Layer 1 — Environment variables (must be set BEFORE any audio lib loads)
os.environ.setdefault("ALSA_LOG_LEVEL", "0")
os.environ.setdefault("JACK_NO_START_SERVER", "1")
os.environ.setdefault("JACK_NO_AUDIO_RESERVATION", "1")

# Layer 2 — ctypes ALSA error handler
_ALSA_HANDLER = None      # prevent garbage collection
_ASOUND_LIB = None        # prevent garbage collection

try:
    import ctypes

    _ALSA_HANDLER_TYPE = ctypes.CFUNCTYPE(
        None,                # void return
        ctypes.c_char_p,     # filename
        ctypes.c_int,        # line
        ctypes.c_char_p,     # function
        ctypes.c_int,        # err
        ctypes.c_char_p,     # fmt
    )

    @_ALSA_HANDLER_TYPE
    def _null_alsa_handler(filename, line, function, err, fmt):
        """Swallow all ALSA diagnostic messages — pinned at module scope."""
        pass

    _ALSA_HANDLER = _null_alsa_handler  # prevent GC

    try:
        _ASOUND_LIB = ctypes.cdll.LoadLibrary("libasound.so.2")
        _ASOUND_LIB.snd_lib_error_set_handler.argtypes = [_ALSA_HANDLER_TYPE]
        _ASOUND_LIB.snd_lib_error_set_handler.restype = ctypes.c_int
        _ASOUND_LIB.snd_lib_error_set_handler(_ALSA_HANDLER)
    except OSError:
        pass
except Exception:
    pass

# Layer 3 — Silence stderr during PyAudio import
_devnull = os.open(os.devnull, os.O_WRONLY)
_saved_stderr = os.dup(2)
try:
    os.dup2(_devnull, 2)
    import pyaudio
finally:
    os.dup2(_saved_stderr, 2)
    os.close(_devnull)
    os.close(_saved_stderr)


import array as _array  # fast PCM unpacking for level meter
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000        # 16 kHz — military-grade voice clarity
SAMPLE_WIDTH = 2    # 16-bit = 2 bytes per sample
VOLUME_GAIN = 4.0   # multiplier applied to received audio for audibility


# ═══════════════════════════════════════════════════════════════════════════
# COLOUR PALETTE
# ═══════════════════════════════════════════════════════════════════════════
C_BG           = "#0d0d0d"
C_PANEL        = "#141414"
C_CARD         = "#1a1a2e"
C_BORDER       = "#2a2a3e"
C_TEXT         = "#e0e0e0"
C_DIM          = "#6b7b8d"
C_ACCENT       = "#00e5ff"
C_ACCENT_DIM   = "#007a8a"
C_TALK_IDLE    = "#2a2a2a"
C_TALK_BORDER  = "#3a3a4a"
C_TALK_ACTIVE  = "#ff1744"
C_TALK_GLOW    = "#ff5252"
C_ONLINE       = "#00e676"
C_OFFLINE      = "#ff1744"
C_METER_LOW    = "#00e676"
C_METER_MID    = "#ffea00"
C_METER_HIGH   = "#ff1744"
C_METER_BG     = "#1e1e1e"


# ═══════════════════════════════════════════════════════════════════════════
# APPLICATION
# ═══════════════════════════════════════════════════════════════════════════
class TalkieWalkieRadio:
    """Professional PTT radio client with high-tech dark UI."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Talkie-Walkie  ·  PTT Radio")
        self.root.geometry("440x680")
        self.root.configure(bg=C_BG)
        self.root.resizable(False, False)

        # ── State ────────────────────────────────────────────────────
        self.is_talking = False
        self.is_connected = False
        self.pa = pyaudio.PyAudio()
        self.sock: socket.socket | None = None
        self.server_addr: tuple | None = None
        self._stop = threading.Event()
        self._level = 0.0            # 0.0 – 1.0 normalised audio level
        self._meter_after_id = None
        self._waveform_samples = []  # latest PCM samples for waveform drawing

        # ── Build UI ─────────────────────────────────────────────────
        self._build_ui()

        # ── Key bindings ─────────────────────────────────────────────
        self.root.bind("<KeyPress-space>", self._on_ptt_press)
        self.root.bind("<KeyRelease-space>", self._on_ptt_release)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ──────────────────────────────────────────────────────────────────
    # UI
    # ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # ── Title bar ────────────────────────────────────────────────
        title_frame = tk.Frame(self.root, bg=C_BG)
        title_frame.pack(fill="x", padx=24, pady=(20, 0))

        tk.Label(
            title_frame, text="TALKIE-WALKIE", font=("Consolas", 22, "bold"),
            fg=C_ACCENT, bg=C_BG,
        ).pack(side="left")

        # Status dot (top-right)
        self._status_canvas = tk.Canvas(
            title_frame, width=16, height=16, bg=C_BG, highlightthickness=0,
        )
        self._status_canvas.pack(side="right", pady=4)
        self._status_dot = self._status_canvas.create_oval(2, 2, 14, 14,
                                                            fill=C_OFFLINE, outline="")

        tk.Label(
            title_frame, text="PTT RADIO", font=("Consolas", 9),
            fg=C_DIM, bg=C_BG,
        ).pack(side="right", padx=(0, 8))

        # Thin accent line
        tk.Frame(self.root, bg=C_ACCENT_DIM, height=1).pack(fill="x", padx=24, pady=(10, 0))

        # ── Connection panel ─────────────────────────────────────────
        conn_frame = tk.Frame(self.root, bg=C_CARD, highlightbackground=C_BORDER,
                              highlightthickness=1)
        conn_frame.pack(fill="x", padx=24, pady=(14, 0))

        inner = tk.Frame(conn_frame, bg=C_CARD)
        inner.pack(fill="x", padx=16, pady=12)

        tk.Label(inner, text="SERVER", font=("Consolas", 9, "bold"),
                 fg=C_DIM, bg=C_CARD).pack(anchor="w")

        # Host entry
        row1 = tk.Frame(inner, bg=C_CARD)
        row1.pack(fill="x", pady=(6, 3))
        tk.Label(row1, text="HOST", font=("Consolas", 8), fg=C_DIM,
                 bg=C_CARD, width=6, anchor="w").pack(side="left")
        self._host_var = tk.StringVar(value="127.0.0.1")
        tk.Entry(
            row1, textvariable=self._host_var, font=("Consolas", 11),
            bg="#0a0a18", fg=C_TEXT, insertbackground=C_ACCENT,
            relief="flat", highlightthickness=1,
            highlightbackground=C_BORDER, highlightcolor=C_ACCENT,
        ).pack(side="left", fill="x", expand=True, ipady=4, padx=(4, 0))

        # Port entry
        row2 = tk.Frame(inner, bg=C_CARD)
        row2.pack(fill="x", pady=(3, 0))
        tk.Label(row2, text="PORT", font=("Consolas", 8), fg=C_DIM,
                 bg=C_CARD, width=6, anchor="w").pack(side="left")
        self._port_var = tk.StringVar(value="9000")
        tk.Entry(
            row2, textvariable=self._port_var, font=("Consolas", 11),
            bg="#0a0a18", fg=C_TEXT, insertbackground=C_ACCENT,
            relief="flat", highlightthickness=1,
            highlightbackground=C_BORDER, highlightcolor=C_ACCENT,
        ).pack(side="left", fill="x", expand=True, ipady=4, padx=(4, 0))

        # Connect button + status
        btn_row = tk.Frame(inner, bg=C_CARD)
        btn_row.pack(fill="x", pady=(10, 0))

        self._connect_btn = tk.Button(
            btn_row, text="▶  CONNECT", font=("Consolas", 10, "bold"),
            bg=C_ACCENT_DIM, fg="#ffffff", activebackground=C_ACCENT,
            activeforeground="#000000", relief="flat", cursor="hand2",
            padx=14, pady=4, command=self._toggle_connect,
        )
        self._connect_btn.pack(side="left")

        self._conn_label = tk.Label(
            btn_row, text="OFFLINE", font=("Consolas", 9),
            fg=C_OFFLINE, bg=C_CARD,
        )
        self._conn_label.pack(side="left", padx=(12, 0))

        # ── Level meter (waveform + bar hybrid) ────────────────────
        meter_frame = tk.Frame(self.root, bg=C_BG)
        meter_frame.pack(fill="x", padx=24, pady=(14, 0))

        tk.Label(meter_frame, text="AUDIO LEVEL", font=("Consolas", 8, "bold"),
                 fg=C_DIM, bg=C_BG).pack(anchor="w")

        # Waveform canvas — shows live PCM waveform
        self._wave_canvas = tk.Canvas(
            meter_frame, height=48, bg=C_METER_BG,
            highlightbackground=C_BORDER, highlightthickness=1,
        )
        self._wave_canvas.pack(fill="x", pady=(4, 0))

        # Bar meter canvas below waveform
        self._meter_canvas = tk.Canvas(
            meter_frame, height=18, bg=C_METER_BG,
            highlightbackground=C_BORDER, highlightthickness=1,
        )
        self._meter_canvas.pack(fill="x", pady=(2, 0))

        self._meter_bars = []
        self._meter_count = 28
        self._meter_canvas.bind("<Configure>", self._draw_meter_bars)

        # ── Talk button area ─────────────────────────────────────────
        talk_frame = tk.Frame(self.root, bg=C_BG)
        talk_frame.pack(fill="both", expand=True, padx=24, pady=(10, 0))

        self._talk_status = tk.Label(
            talk_frame, text="STANDBY", font=("Consolas", 11, "bold"),
            fg=C_DIM, bg=C_BG,
        )
        self._talk_status.pack(pady=(14, 6))

        # Circular talk button using Canvas
        btn_size = 180
        self._talk_canvas = tk.Canvas(
            talk_frame, width=btn_size + 20, height=btn_size + 20,
            bg=C_BG, highlightthickness=0,
        )
        self._talk_canvas.pack(pady=4)

        cx, cy = (btn_size + 20) // 2, (btn_size + 20) // 2

        # Outer glow ring
        self._glow_ring = self._talk_canvas.create_oval(
            cx - btn_size // 2 - 6, cy - btn_size // 2 - 6,
            cx + btn_size // 2 + 6, cy + btn_size // 2 + 6,
            fill="", outline=C_TALK_BORDER, width=2,
        )

        # Main circle
        self._talk_circle = self._talk_canvas.create_oval(
            cx - btn_size // 2, cy - btn_size // 2,
            cx + btn_size // 2, cy + btn_size // 2,
            fill=C_TALK_IDLE, outline=C_TALK_BORDER, width=2,
        )

        # Mic icon text
        self._talk_icon = self._talk_canvas.create_text(
            cx, cy - 16, text="🎙", font=("Segoe UI Emoji", 32), fill=C_DIM,
        )
        self._talk_text = self._talk_canvas.create_text(
            cx, cy + 30, text="TALK", font=("Consolas", 18, "bold"), fill=C_DIM,
        )

        # Bind mouse events on the canvas
        self._talk_canvas.bind("<ButtonPress-1>", self._on_ptt_press)
        self._talk_canvas.bind("<ButtonRelease-1>", self._on_ptt_release)

        # Instruction
        self._hint_label = tk.Label(
            talk_frame, text="Hold button or SPACEBAR to transmit",
            font=("Consolas", 8), fg=C_DIM, bg=C_BG,
        )
        self._hint_label.pack(pady=(6, 0))

        # ── Footer ───────────────────────────────────────────────────
        footer = tk.Frame(self.root, bg=C_PANEL, height=36)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)

        tk.Label(
            footer, text="16kHz · Mono · 16-bit  │  UDP",
            font=("Consolas", 8), fg=C_DIM, bg=C_PANEL,
        ).pack(side="left", padx=12, pady=8)

        self._fps_label = tk.Label(
            footer, text="", font=("Consolas", 8), fg=C_DIM, bg=C_PANEL,
        )
        self._fps_label.pack(side="right", padx=12, pady=8)

    # ── Meter drawing ────────────────────────────────────────────────
    def _draw_meter_bars(self, event=None):
        """Create the bar segments for the level meter."""
        self._meter_canvas.delete("bars")
        w = self._meter_canvas.winfo_width()
        h = self._meter_canvas.winfo_height()
        if w < 10:
            return

        n = self._meter_count
        gap = 2
        bar_w = max(1, (w - (n + 1) * gap) // n)
        self._meter_bars = []

        for i in range(n):
            x0 = gap + i * (bar_w + gap)
            x1 = x0 + bar_w
            bar = self._meter_canvas.create_rectangle(
                x0, 3, x1, h - 3, fill=C_METER_BG, outline="", tags="bars",
            )
            self._meter_bars.append(bar)

    def _update_meter(self, level: float):
        """Update meter bars based on normalised level 0.0–1.0."""
        n = len(self._meter_bars)
        if n == 0:
            return
        lit = int(level * n)
        for i, bar in enumerate(self._meter_bars):
            if i < lit:
                frac = i / n
                if frac < 0.5:
                    col = C_METER_LOW
                elif frac < 0.8:
                    col = C_METER_MID
                else:
                    col = C_METER_HIGH
            else:
                col = C_METER_BG
            self._meter_canvas.itemconfig(bar, fill=col)

    def _draw_waveform(self):
        """Draw a live waveform on the wave canvas from current PCM samples."""
        self._wave_canvas.delete("wave")
        w = self._wave_canvas.winfo_width()
        h = self._wave_canvas.winfo_height()
        if w < 10 or not self._waveform_samples:
            # Mock idle waveform
            mid = h // 2
            t = time.time() * 5
            points = []
            for x in range(w):
                y = mid + math.sin(x * 0.05 + t) * 3
                points.extend([x, y])
            if points:
                self._wave_canvas.create_line(
                    *points, fill="#007a8a", width=1.5, smooth=True, tags="wave",
                )
            return

        samples = self._waveform_samples
        n = len(samples)
        mid = h // 2
        max_amp = h // 2 - 2  # leave 2px padding

        # Downsample to canvas width
        step = max(1, n // w)
        points = []
        for i in range(0, min(n, w * step), step):
            x = len(points)
            # Normalise sample (-32768..32767) to canvas y
            s = samples[i] / 32768.0
            y = mid - int(s * max_amp)
            y = max(1, min(h - 1, y))
            points.append(x)
            points.append(y)

        if len(points) >= 4:
            self._wave_canvas.create_line(
                *points, fill=C_ACCENT, width=2, smooth=True, tags="wave",
            )

    # ── Meter animation loop ─────────────────────────────────────────
    def _meter_tick(self):
        """Called ~30fps on the main thread to update level meter + waveform."""
        self._update_meter(self._level)
        self._draw_waveform()
        # Decay level
        self._level *= 0.82
        if self._level < 0.01:
            self._level = 0.0
            self._waveform_samples = []  # clear waveform when silent
        self._meter_after_id = self.root.after(33, self._meter_tick)

    # ──────────────────────────────────────────────────────────────────
    # CONNECTION
    # ──────────────────────────────────────────────────────────────────
    def _toggle_connect(self):
        if self.is_connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        host = self._host_var.get().strip()
        try:
            port = int(self._port_var.get().strip())
        except ValueError:
            return

        # ── Log audio device info for debugging ──────────────────
        default_in = self.pa.get_default_input_device_info()
        default_out = self.pa.get_default_output_device_info()
        print(f"[AUDIO] Input device:  #{default_in['index']} — {default_in['name']}")
        print(f"[AUDIO] Output device: #{default_out['index']} — {default_out['name']}")
        print(f"[AUDIO] Format: {RATE}Hz, {'Mono' if CHANNELS == 1 else 'Stereo'}, 16-bit")
        print(f"[AUDIO] Chunk: {CHUNK} frames = {CHUNK / RATE * 1000:.0f}ms")

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server_addr = (host, port)
        self._stop.clear()

        # Send a registration ping so server knows about us
        try:
            self.sock.sendto(b"PING", self.server_addr)
            print(f"[NET]   Registered with server {host}:{port}")
        except OSError as e:
            print(f"[NET]   Failed to register: {e}")
            return

        self.is_connected = True

        # Start receiver thread
        threading.Thread(target=self._receive_loop, daemon=True).start()

        # Start meter animation
        self._meter_tick()

        # Update UI
        self._connect_btn.config(text="■  DISCONNECT", bg=C_OFFLINE)
        self._conn_label.config(text="ONLINE", fg=C_ONLINE)
        self._status_canvas.itemconfig(self._status_dot, fill=C_ONLINE)
        self._talk_canvas.itemconfig(self._talk_text, fill=C_TEXT)
        self._talk_canvas.itemconfig(self._talk_icon, fill=C_TEXT)
        self._talk_status.config(text="READY", fg=C_ONLINE)

    def _disconnect(self):
        self._stop.set()
        self.is_connected = False
        self.is_talking = False

        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

        # Stop meter
        if self._meter_after_id:
            self.root.after_cancel(self._meter_after_id)
            self._meter_after_id = None
        self._level = 0.0
        self._update_meter(0.0)

        # Update UI
        self._connect_btn.config(text="▶  CONNECT", bg=C_ACCENT_DIM)
        self._conn_label.config(text="OFFLINE", fg=C_OFFLINE)
        self._status_canvas.itemconfig(self._status_dot, fill=C_OFFLINE)
        self._talk_canvas.itemconfig(self._talk_circle, fill=C_TALK_IDLE, outline=C_TALK_BORDER)
        self._talk_canvas.itemconfig(self._glow_ring, outline=C_TALK_BORDER)
        self._talk_canvas.itemconfig(self._talk_text, fill=C_DIM)
        self._talk_canvas.itemconfig(self._talk_icon, fill=C_DIM)
        self._talk_status.config(text="STANDBY", fg=C_DIM)

    # ──────────────────────────────────────────────────────────────────
    # AUDIO — RECEIVER (background thread)
    # ──────────────────────────────────────────────────────────────────
    def _receive_loop(self):
        """Continuously receive UDP audio and play it."""
        # Auto-detect the real default output device
        default_out = self.pa.get_default_output_device_info()
        out_idx = int(default_out['index'])
        print(f"[RX]    Auto-detected output: #{out_idx} — {default_out['name']}")

        # List all output devices for debugging
        for i in range(self.pa.get_device_count()):
            info = self.pa.get_device_info_by_index(i)
            if info['maxOutputChannels'] > 0:
                marker = " ◀ SELECTED" if i == out_idx else ""
                print(f"[RX]      Output #{i}: {info['name']} ({info['maxOutputChannels']}ch){marker}")

        spk = self.pa.open(
            format=FORMAT, channels=CHANNELS, rate=RATE,
            output=True, frames_per_buffer=CHUNK,
            output_device_index=out_idx,
        )
        print(f"[RX]    Speaker stream opened — ready to play")
        rx_count = 0
        try:
            while not self._stop.is_set():
                try:
                    data, addr = self.sock.recvfrom(CHUNK * 4)
                except (OSError, AttributeError):
                    break
                if len(data) < 32:
                    continue  # skip control packets
                try:
                    # ── Volume Gain ── amplify received audio
                    amplified = self._apply_gain(data, VOLUME_GAIN)
                    spk.write(amplified)
                    rx_count += 1
                    if rx_count <= 5 or rx_count % 100 == 0:
                        print(f"[RX]    Playing chunk #{rx_count} ({len(data)}B from {addr[0]}:{addr[1]})")
                    # Compute level for meter (incoming audio)
                    if not self.is_talking:
                        self._compute_level(amplified)
                except Exception as e:
                    print(f"[RX]    Playback error: {e}")
        finally:
            print(f"[RX]    Receiver stopped after {rx_count} chunks")
            try:
                spk.stop_stream()
                spk.close()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────
    # AUDIO — SENDER (background thread, runs while talking)
    # ──────────────────────────────────────────────────────────────────
    def _send_loop(self):
        """Capture mic and send via UDP while is_talking is True."""
        default_in = self.pa.get_default_input_device_info()
        mic = self.pa.open(
            format=FORMAT, channels=CHANNELS, rate=RATE,
            input=True, frames_per_buffer=CHUNK,
            input_device_index=default_in['index'],
        )
        print(f"[TX]    Mic stream opened on device #{default_in['index']}")
        tx_count = 0
        try:
            while self.is_talking and self.is_connected:
                try:
                    data = mic.read(CHUNK, exception_on_overflow=False)
                    self.sock.sendto(data, self.server_addr)
                    tx_count += 1
                    if tx_count <= 5 or tx_count % 100 == 0:
                        print(f"[TX]    Sent chunk #{tx_count} ({len(data)} bytes)")
                    self._compute_level(data)
                except Exception as e:
                    print(f"[TX]    Send error: {e}")
                    break
        finally:
            print(f"[TX]    Transmit stopped after {tx_count} chunks")
            try:
                mic.stop_stream()
                mic.close()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────
    # AUDIO PROCESSING
    # ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _apply_gain(data: bytes, gain: float) -> bytes:
        """Multiply PCM samples by gain, clamp to int16 range."""
        samples = _array.array('h', data)
        for i in range(len(samples)):
            amplified = int(samples[i] * gain)
            # Clamp to 16-bit signed range
            if amplified > 32767:
                amplified = 32767
            elif amplified < -32768:
                amplified = -32768
            samples[i] = amplified
        return samples.tobytes()

    def _compute_level(self, data: bytes):
        """Compute RMS level from raw PCM data — updates meter AND waveform."""
        try:
            samples = _array.array('h', data)
            n = len(samples)
            if n == 0:
                return

            # Store samples for waveform drawing (last chunk only)
            self._waveform_samples = list(samples)

            # RMS calculation
            sum_sq = sum(s * s for s in samples)
            rms = math.sqrt(sum_sq / n)

            # Normalise: using 1500 as divisor to make meter very responsive
            # to normal speech levels (typical RMS 300–2000)
            normalised = min(1.0, rms / 1500.0)

            # Only update upward (meter_tick handles decay)
            if normalised > self._level:
                self._level = normalised
        except Exception as e:
            print(f"[METER] Level compute error: {e}")

    # ──────────────────────────────────────────────────────────────────
    # PTT EVENTS
    # ──────────────────────────────────────────────────────────────────
    def _on_ptt_press(self, event=None):
        if not self.is_connected or self.is_talking:
            return
        self.is_talking = True

        # Visual feedback — neon red glow
        self._talk_canvas.itemconfig(self._talk_circle, fill=C_TALK_ACTIVE, outline=C_TALK_GLOW)
        self._talk_canvas.itemconfig(self._glow_ring, outline=C_TALK_ACTIVE)
        self._talk_canvas.itemconfig(self._talk_text, fill="#ffffff")
        self._talk_canvas.itemconfig(self._talk_icon, fill="#ffffff")
        self._talk_status.config(text="● TRANSMITTING", fg=C_TALK_ACTIVE)

        threading.Thread(target=self._send_loop, daemon=True).start()

    def _on_ptt_release(self, event=None):
        if not self.is_talking:
            return
        self.is_talking = False

        # Visual feedback — back to idle
        if self.is_connected:
            self._talk_canvas.itemconfig(self._talk_circle, fill=C_TALK_IDLE, outline=C_TALK_BORDER)
            self._talk_canvas.itemconfig(self._glow_ring, outline=C_TALK_BORDER)
            self._talk_canvas.itemconfig(self._talk_text, fill=C_TEXT)
            self._talk_canvas.itemconfig(self._talk_icon, fill=C_TEXT)
            self._talk_status.config(text="READY", fg=C_ONLINE)

    # ──────────────────────────────────────────────────────────────────
    # CLEANUP
    # ──────────────────────────────────────────────────────────────────
    def _on_close(self):
        self._disconnect()
        try:
            self.pa.terminate()
        except Exception:
            pass
        self.root.destroy()


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    root = tk.Tk()
    app = TalkieWalkieRadio(root)
    root.mainloop()