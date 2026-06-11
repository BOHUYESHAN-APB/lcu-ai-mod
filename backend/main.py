"""
LCUMod Backend — LLM Control User

Starts the web dashboard + wire client + orchestrator in one process.
Architecture: Session (集中管理) → Orchestrator → WireClient → Java Mod
"""

import os
import logging
import threading
import time

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

    port = int(os.getenv("WEB_PORT", "8080"))
    logger.info("Starting LCUMod backend on port %d...", port)

    import uvicorn
    uvicorn.run("server:app", port=port, host="0.0.0.0", reload=False)
