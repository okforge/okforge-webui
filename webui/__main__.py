"""Standalone server: ``python -m webui`` serves the frontend, the API,
and the MCP server in one uvicorn process — no Apache required.

Bind address/port come from the environment (all other configuration:
see config.py):

    OPENKB_WEBUI_HOST   default 0.0.0.0  (LAN-visible; use 127.0.0.1 to
                        keep it local or to put a reverse proxy in front)
    OPENKB_WEBUI_PORT   default 8500

Same trust model as the Apache deployment: LAN-only, no auth — don't
expose it beyond a network you trust.
"""

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "webui.api:app",
        host=os.environ.get("OPENKB_WEBUI_HOST", "0.0.0.0"),
        port=int(os.environ.get("OPENKB_WEBUI_PORT", "8500")),
    )


if __name__ == "__main__":
    main()
