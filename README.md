# Prism

Transparent LLM reverse proxy. One port. Full logging. Easy backend switching.

Designed by SK. Built by Claude.

## Why

If you run local LLMs, you know the pain. Multiple clients pointing at your backend. Swap the model and you're editing configs everywhere. Switch from vLLM to llama.cpp and everything breaks. And you have no idea what any of these tools are actually sending.

Prism fixes this. One port, all clients. Switch anything behind it — they never know.

## Quick Start

```bash
git clone https://github.com/sunwoo85/prism.git
cd prism
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python prism.py
```

Listening on `:1319`, forwarding to `localhost:8000`.

## How It Works

```
Clients ──► :1319 (prism) ──► :8000 (your LLM backend)
                │
                └──► logs/YYYY-MM-DD/*.json
```

Any OpenAI-compatible client. Any OpenAI-compatible backend. ~330 lines, three dependencies, one file.

## Features

- **Transparent proxy** — any client to any backend, zero latency overhead
- **Full logging** — every request and response as structured JSON
- **Streaming-aware** — SSE passthrough, chunks reassembled post-response for logging
- **Token counting** — OpenAI, Anthropic, and llama.cpp formats
- **Multi-backend switching** — swap between models on the fly
- **Process manager** — start, stop, restart, status, switch, logs

## Configuration

```bash
cp prism.example.conf prism.conf
```

| Setting | Default | Purpose |
|---------|---------|---------|
| `BACKEND_URL` | `http://localhost:8000` | LLM backend |
| `LISTEN_PORT` | `1319` | Proxy port |
| `LOG_DIR` | `./logs` | Log directory |
| `BACKEND_TIMEOUT` | `14400` | Timeout in seconds |

## Multi-Backend Switching

```bash
./start.sh switch       # toggle between backends
./start.sh switch 2     # switch to backend 2
./start.sh status       # show current backend and model
```

Define backends in `prism.conf`:

```bash
BACKEND_1_URL="http://localhost:8000"
BACKEND_1_LABEL="Backend 1"
BACKEND_2_URL="http://localhost:8080"
BACKEND_2_LABEL="Backend 2"
```

## Logging

Every request is saved as a JSON file under `logs/YYYY-MM-DD/`. Each log contains the full request (method, path, headers, body), response (status, body), and metadata (duration, model, token counts, client IP, stream flag). Streaming responses are reassembled post-response so logs always contain the complete text.

## Roadmap

- [ ] Dashboard — real-time traffic visualization
- [ ] Rewrite engine — per-client request modification
- [ ] Cortex capture — external conversation logging

## Version History

| Version | Date | Description |
|---------|------|-------------|
| 0.1.0 | 2026-03-29 | Initial release — transparent proxy, full logging, multi-backend switching |

## License

MIT
