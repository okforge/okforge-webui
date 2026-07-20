"""Standalone server: ``python -m webui`` serves the frontend, the API,
and the MCP server in one uvicorn process — no Apache required.

Bind address/port come from the environment (all other configuration:
see config.py):

    OKFORGE_WEBUI_HOST  default 0.0.0.0  (LAN-visible; use 127.0.0.1 to
                        keep it local or to put a reverse proxy in front)
    OKFORGE_WEBUI_PORT  default 8500

(The pre-rebrand OPENKB_WEBUI_HOST/PORT names still work — see config._env.)

Same trust model as the Apache deployment: LAN-only, no auth — don't
expose it beyond a network you trust.
"""

import uvicorn

from webui import config


def main() -> None:
    uvicorn.run("webui.api:app", host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
