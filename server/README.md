# Roblox RL Gym Server

FastAPI-based server that manages Roblox Studio instances and coordinates delta evaluation for reinforcement learning.

## Overview

The server component of Roblox RL Gym provides a REST API for evaluating DataModel deltas and manages the lifecycle of Roblox Studio instances. It handles plugin installation, Studio configuration, delta distribution via Server-Sent Events (SSE), screenshot capture, and result collection.

## Features

- **Delta Evaluation**: Evaluate multiple deltas against a place file with before/after screenshots
- **Automatic Studio Management**: Launches and manages Roblox Studio processes
- **Plugin Installation**: Automatically builds and installs the delta manager plugin
- **FFlag Configuration**: Sets required Studio flags for SSE support
- **Screenshot Capture**: Captures Studio window screenshots via Windows API
- **Real-time Communication**: SSE-based bidirectional communication with plugin
- **Error Recovery**: Graceful handling of Studio crashes and network failures
- **Authentication System**: Dual authentication with API keys for remote workers and session tokens for plugin
- **Configurable Output**: PNG or JPEG screenshots with adjustable quality

## Installation

### Prerequisites

- Python 3.11 or higher
- [UV](https://github.com/astral-sh/uv) package manager
- [Rojo](https://rojo.space/) for building Roblox files
- Windows OS (for Roblox Studio and screenshot capture)

### Setup

1. **Install UV** (if not already installed):
   ```bash
   pip install uv
   ```

2. **Install dependencies**:
   ```bash
   cd server
   uv pip install -e .
   ```

3. **Install development dependencies** (optional):
   ```bash
   uv pip install -e ".[dev]"
   ```

## Usage

### Setting Up Authentication

1. **Create API keys file** (`server/api_keys.txt`):
   ```
   # Add one API key per line
   worker1_key_abc123xyz789
   worker2_key_def456uvw012
   ```

2. **Generate secure API keys**:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

### Starting the Server

**Using the run script** (recommended):
```bash
cd server
uv run python run.py
```

**With custom configuration**:
```bash
ROBLOX_RL_GYM_PORT=8080 ROBLOX_RL_GYM_LOG_LEVEL=DEBUG uv run python run.py
```

**Disable authentication** (for local development only):
```bash
ROBLOX_RL_GYM_ENABLE_AUTH=false uv run python run.py
```

### Server Startup Process

When the server starts, it:

1. **Loads API Keys**: Reads API keys from `api_keys.txt` for remote worker authentication
2. **Generates Session Token**: Creates a unique token for plugin internal endpoints
3. **Configures Studio**: Sets required FFlags in Studio's ClientSettings via FFlagManager
4. **Installs the Plugin**: Builds and installs the Roblox Studio plugin with embedded session token
5. **Ready for Requests**: Begins accepting evaluate requests

### Evaluating Deltas

```python
import requests
import json

# Prepare the request
with open("my_place.rbxl", "rb") as f:
    place_data = f.read()

deltas = [
    "delta-string-1",
    "delta-string-2",
    "delta-string-3",
]

response = requests.post(
    "http://localhost:8325/evaluate",
    files={"place_file": ("place.rbxl", place_data)},
    data={"deltas": json.dumps(deltas)},
    headers={"X-API-Key": "your-api-key-here"},
)

result = response.json()
print(f"Success: {result['success']}")
print(f"Before screenshot: {result['before_screenshot'][:50]}...")

for delta_result in result['results']:
    print(f"Delta {delta_result['delta_index']}: {delta_result['success']}")
```

## API Reference

### Endpoints

#### `POST /evaluate`
Evaluate a list of deltas against a place file. For each delta, opens the place in Studio, captures before/after screenshots.

**Request (multipart/form-data):**
- `place_file`: The `.rbxl` place file (required)
- `deltas`: JSON array of delta strings (required)

**Headers:**
- `X-API-Key: your-api-key` (required if auth enabled)

**Response (200 OK):**
```json
{
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "success": true,
  "error": null,
  "before_screenshot": "base64-encoded-image...",
  "results": [
    {
      "delta_index": 0,
      "success": true,
      "error": null,
      "after_screenshot": "base64-encoded-image..."
    }
  ],
  "timestamp": "2025-12-16T..."
}
```

#### `GET /health`
Check server status.

**Response (idle):**
```json
{
  "status": "healthy",
  "studio_active": false
}
```

**Response (during evaluation):**
```json
{
  "status": "healthy",
  "studio_active": true,
  "studio_running": true,
  "plugin_installed": true,
  "plugin_connected": true,
  "fflags_applied": true,
  "place_file_exists": true
}
```

#### `GET /_events` (Internal)
Server-Sent Events stream for plugin communication. Protected by session token.

**Event Types:**
- `ping`: Keepalive message
- `delta_apply`: Apply a delta string

#### `POST /_delta_result` (Internal)
Receive delta application results from plugin. Protected by session token.

**Request:**
```json
{
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "success": true,
  "error": null
}
```

#### `POST /_heartbeat` (Internal)
Receive heartbeat from plugin. Protected by session token.

## Configuration

### Environment Variables

All environment variables use the prefix `ROBLOX_RL_GYM_`:

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `127.0.0.1` | Server bind address |
| `PORT` | `8325` | Server port |
| `STEP_TIMEOUT` | `30` | Delta application timeout (seconds) |
| `SHUTDOWN_TIMEOUT` | `30` | Graceful shutdown timeout (seconds) |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `ENABLE_AUTH` | `true` | Enable authentication system |
| `CORS_ORIGINS` | `["*"]` | Allowed CORS origins (JSON array) |
| `SCREENSHOT_WIDTH` | `512` | Output screenshot width (pixels) |
| `SCREENSHOT_HEIGHT` | `512` | Output screenshot height (pixels) |
| `SCREENSHOT_FORMAT` | `png` | Image format: `png` or `jpeg` |
| `SCREENSHOT_JPEG_QUALITY` | `85` | JPEG quality (1-100) |
| `SCREENSHOT_CROP_LEFT` | `0.15` | Viewport crop from left (0.0-1.0) |
| `SCREENSHOT_CROP_RIGHT` | `0.20` | Viewport crop from right (0.0-1.0) |
| `SCREENSHOT_CROP_TOP` | `0.08` | Viewport crop from top (0.0-1.0) |
| `SCREENSHOT_CROP_BOTTOM` | `0.15` | Viewport crop from bottom (0.0-1.0) |

### Configuration File

Create a `.env` file in the server directory:
```env
ROBLOX_RL_GYM_HOST=0.0.0.0
ROBLOX_RL_GYM_PORT=8080
ROBLOX_RL_GYM_STEP_TIMEOUT=60
ROBLOX_RL_GYM_LOG_LEVEL=DEBUG

# Screenshot settings for model training
ROBLOX_RL_GYM_SCREENSHOT_WIDTH=256
ROBLOX_RL_GYM_SCREENSHOT_HEIGHT=256
ROBLOX_RL_GYM_SCREENSHOT_FORMAT=jpeg
ROBLOX_RL_GYM_SCREENSHOT_JPEG_QUALITY=80
```

### Screenshot Configuration

Screenshots are automatically cropped to remove Studio UI panels and resized for consistent model input. The default crop percentages are tuned for a typical Studio layout:

- **Left (15%)**: Removes Explorer panel and Toolbox
- **Right (20%)**: Removes Properties panel  
- **Top (8%)**: Removes menu bar and ribbon
- **Bottom (15%)**: Removes Output window and command bar

Adjust these values based on your Studio layout. Set `SCREENSHOT_WIDTH` and `SCREENSHOT_HEIGHT` to `null` to disable resizing and return the cropped viewport at original resolution.

### Studio FFlags

The server automatically configures these FFlags in `ClientSettings/ClientAppSettings.json`:

```json
{
  "FFlagEnableLoadModule": "true"
}
```

## Components

### FFlagManager

Manages Roblox Studio FFlag configuration:
- Applies required FFlags for SSE streaming
- Backs up existing FFlag configuration
- Restores original flags on shutdown
- Provides context manager for automatic cleanup

### PluginManager

Handles plugin lifecycle:
- Builds plugin from source using Rojo
- Injects server configuration
- Installs to Studio plugins directory
- Manages plugin updates and removal
- Provides context manager for automatic cleanup

### StudioManager

Manages Roblox Studio process:
- Locates Studio installation via registry and filesystem
- Launches and monitors Studio process
- Handles graceful shutdown
- Provides unified health checking across all components

### Screenshot Capture

Captures the Studio window state:
- Uses Windows API to find Studio window
- Captures using `mss` library
- Supports PNG and JPEG output formats
- Returns base64-encoded images
- Handles minimized/obscured windows

## Development

### Project Structure

```
server/
├── app/                        # Application code
│   ├── __init__.py
│   ├── main.py                 # FastAPI app and lifecycle
│   ├── config_manager.py       # Settings management
│   ├── dependencies.py         # Dependency injection
│   ├── auth.py                 # Authentication middleware
│   ├── api_keys.py             # API key management
│   ├── endpoints/              # API endpoints
│   │   ├── __init__.py
│   │   ├── evaluate.py         # /evaluate endpoint
│   │   ├── events.py           # SSE endpoint (session token)
│   │   └── delta_results.py    # Results collection (session token)
│   └── utils/                  # Utilities
│       ├── __init__.py
│       ├── screenshot_capture.py  # Windows screenshot capture
│       ├── fflag_manager.py    # FFlag management
│       ├── plugin_manager.py   # Plugin management
│       └── studio_manager.py   # Studio management
├── api_keys.txt                # API keys (gitignored)
├── api_keys.txt.example        # Example API keys
├── pyproject.toml              # Project config
├── uv.lock                     # Locked deps
└── run.py                      # Entry point
```

### Debugging

Enable debug logging:
```bash
ROBLOX_RL_GYM_LOG_LEVEL=DEBUG uv run python run.py
```

Monitor Studio output:
- Check Studio's output window for plugin logs
- Review server logs for communication issues

Common issues:
- **Studio not found**: Check installation path in `studio_manager.py`
- **Plugin not loading**: Verify Rojo is installed and in PATH
- **SSE connection failed**: Check FFlags are properly set
- **Evaluations timing out**: Increase `STEP_TIMEOUT` configuration
- **Screenshot capture failed**: Ensure Studio window is visible

## Error Handling

The server includes comprehensive error handling:

- **Studio Crashes**: Automatically detected and reported
- **Network Failures**: Graceful degradation with error messages
- **Step Timeouts**: Configurable timeouts with clear error responses
- **Plugin Errors**: Captured and returned in response
- **Shutdown**: Graceful cleanup of Studio and plugin

## Security Notes

- **Dual Authentication System**:
  - API keys protect the `/evaluate` endpoint from unauthorized remote workers
  - Session tokens protect internal endpoints (`/_events`, `/_delta_result`, `/_heartbeat`) from external access
- **API Keys Management**:
  - Store in `api_keys.txt` (gitignored)
  - One key per line, comments with `#` supported
  - Generate secure keys with `secrets.token_urlsafe(32)`
- **Session Tokens**:
  - Automatically generated on server startup
  - Injected into plugin configuration
  - Valid only for current server session
- Server binds to localhost by default
- Studio runs with user privileges

## License

This server is part of the Roblox RL Gym project and is licensed under the Apache License 2.0.
