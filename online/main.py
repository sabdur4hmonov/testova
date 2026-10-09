"""Run the online-test website: python -m online.main (port 8002)."""
from __future__ import annotations

import os

import uvicorn

from app.utils.logging import setup_logging


def main() -> None:
    setup_logging()
    uvicorn.run(
        "online.app:app",
        host=os.environ.get("ONLINE_HOST", "0.0.0.0"),
        port=int(os.environ.get("ONLINE_PORT", "8002")),
        workers=1,              # the rate limits are per process (online/ratelimit.py)
        proxy_headers=False,    # X-Forwarded-For is read only from ONLINE_TRUSTED_PROXIES
        log_level="info",
    )


if __name__ == "__main__":
    main()
