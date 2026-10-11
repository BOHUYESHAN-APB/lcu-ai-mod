"""
LCUMod Backend — LLM Control User

Starts the web dashboard + wire client + orchestrator in one process.
Architecture: Session (集中管理) → Orchestrator → WireClient → Java Mod
"""

import os
import logging
import ipaddress

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("wire").setLevel(logging.DEBUG)
logging.getLogger("orchestrator").setLevel(logging.DEBUG)
logging.getLogger("skills").setLevel(logging.DEBUG)
logging.getLogger("modes_engine").setLevel(logging.DEBUG)
logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

logger = logging.getLogger("backend")

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8080"))
    api_token = os.getenv("SDK_API_TOKEN", "").strip()

    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.lower() == "localhost"
    if not is_loopback and not api_token:
        raise RuntimeError("SDK_API_TOKEN is required when WEB_HOST is not loopback")

    logger.info("Starting LCUMod backend on %s:%d...", host, port)

    import uvicorn
    uvicorn.run("server:app", port=port, host=host, reload=False)
