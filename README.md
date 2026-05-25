# 🎙 Talkie-Walkie — Push-to-Talk Voice Chat

A professional Python Push-to-Talk (PTT) application with client-server architecture. Hold the **Talk** button (or **Space bar**) to stream your microphone audio to other connected users in real-time.

## Features

- **Real-time voice streaming** over TCP sockets
- **Push-to-Talk** — hold button to transmit, release to stop
- **Multi-client support** — server relays audio to all connected users
- **Dark-themed UI** built with tkinter
- **Fedora Linux compatible** — handles ALSA/PulseAudio driver noise
- **Low latency** — 16kHz mono PCM, ~64ms chunk size

## Prerequisites (Fedora Linux)

### 1. Install system dependencies

```bash
# PortAudio development headers (required to build PyAudio)
sudo dnf install portaudio-devel

# tkinter (ships separately on Fedora)
sudo dnf install python3-tkinter

# Optional: PulseAudio development headers
sudo dnf install pulseaudio-libs-devel
```

### 2. Create a virtual environment (recommended)

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

## Usage

### Start the Server

```bash
# Default: listens on 0.0.0.0:9000
python server.py

# Custom host/port
python server.py --host 192.168.1.100 --port 8080
```

### Start the Client

```bash
python client.py
```

1. Enter the server **Host** and **Port** in the UI
2. Click **Connect**
3. **Hold** the Talk button (or press **Space bar**) to transmit
4. **Release** to stop transmitting

### Local Testing

Open three terminals:

```bash
# Terminal 1 — Server
python server.py

# Terminal 2 — Client A
python client.py

# Terminal 3 — Client B
python client.py
```

Both clients connect to `127.0.0.1:9000` by default. Hold Talk on one to hear audio on the other.

## Architecture

```
┌──────────┐    TCP    ┌──────────────┐    TCP    ┌──────────┐
│ Client A │ ────────► │ Relay Server │ ────────► │ Client B │
│ (PyAudio │ ◄──────── │  (threading) │ ◄──────── │ (PyAudio │
│ + tkinter│           │              │           │ + tkinter│
└──────────┘           └──────────────┘           └──────────┘
```

**Protocol**: Length-prefixed TCP framing — `[4-byte big-endian length][PCM audio data]`

## Audio Settings

| Parameter   | Value  | Notes                       |
|------------|--------|-----------------------------|
| Sample Rate | 16 kHz | Optimized for voice          |
| Channels   | 1      | Mono                         |
| Bit Depth  | 16-bit | Good quality/bandwidth ratio |
| Chunk Size | 1024   | ~64ms latency per chunk      |

## Troubleshooting

### ALSA errors on Fedora
The client automatically suppresses ALSA diagnostic messages. If you still see errors, ensure PulseAudio or PipeWire is running:
```bash
pulseaudio --check && echo "PulseAudio running" || echo "Not running"
```

### No audio input
Check your default recording device:
```bash
pactl list sources short
```

### Connection refused
Make sure the server is running and firewall allows the port:
```bash
sudo firewall-cmd --add-port=9000/tcp --permanent
sudo firewall-cmd --reload
```
