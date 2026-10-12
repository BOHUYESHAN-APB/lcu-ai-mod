"""
LCUMod Backend — LLM Control User

Starts the web dashboard + wire client + orchestrator in one process.
Architecture: Session (集中管理) → Orchestrator → WireClient → Java Mod
"""

import os
import logging
import ipaddress
from logging.handlers import RotatingFileHandler
from pathlib import Path

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

# 同时落盘一份：用户口径"查一下日志" —— 只打 stdout 的话，控制台一滚就查不到了。
# 位置 `backend/logs/lcu.log`（已 gitignore），带轮转，别无限长。
_log_dir = Path(__file__).resolve().parent / "logs"
try:
    _log_dir.mkdir(parents=True, exist_ok=True)
    _file_handler = RotatingFileHandler(_log_dir / "lcu.log", maxBytes=8 * 1024 * 1024,
                                        backupCount=3, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(_file_handler)
except OSError as exc:  # 落盘失败不该拦住后端启动，但要**说出来**
    logging.getLogger("backend").warning("[backend] 日志文件打不开（只打 stdout）: %s", exc)
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
