"""
LCUMod Backend — LLM Control User

Starts the web dashboard + wire client + orchestrator in one process.
Architecture: Session (集中管理) → Orchestrator → WireClient → Java Mod

关停（用户口径 2026-10-12："意外的那种后端服务器 ctrl 加 c 居然关不掉，只能把整个终端杀掉"）：
`BoundedServer` 在第一次 SIGINT/SIGTERM 后就给优雅关停加上**上限**（`SHUTDOWN_GRACE_SECONDS`），
到点替操作者按下"第二次 Ctrl+C"（uvicorn 自带的 `force_exit`，**不是** `os._exit()`）；
真正收线程/断线/停看门狗在 `server.py` 的 `shutdown` 钩子（`stop_background_work`）。
"""

import ipaddress
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

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

# **第三方库的 DEBUG 噪音**：根 logger 是 DEBUG，会把 httpcore / httpx / asyncio 的逐帧调试
# 也写进日志。实测（2026-10-12，一轮真对话 ~2.9KB 日志）其中 **~2.2KB（约 56%）** 是这些：
# `connect_tcp.started host=… <SyncStream object at 0x…>` / TLS 握手步骤 / 对象 repr ——
# 对排障没价值（真要看 LLM 地址/模型，我们自己会打 `[LLM] Base URL …`）。
# 默认压到 WARNING（错误照旧可见）；要深挖时 `LCU_LOG_LEVEL=DEBUG` 放回全部 DEBUG。
_LIB_LEVEL = (logging.DEBUG if os.getenv("LCU_LOG_LEVEL", "").strip().upper() == "DEBUG"
              else logging.WARNING)
for _noisy in ("httpcore", "httpx", "asyncio", "urllib3", "websockets", "PIL", "multipart",
               "python_multipart", "watchfiles", "charset_normalizer"):
    logging.getLogger(_noisy).setLevel(_LIB_LEVEL)

logger = logging.getLogger("backend")

# 关停**有界**：收到 SIGINT/SIGTERM（Ctrl+C）后最多让优雅关停跑这么久，到点就 `force_exit`
# （uvicorn 自己的机制，等价于"再按一次 Ctrl+C"，**不是** `os._exit()`）。
# 用户口径：Ctrl+C 必须 ≤2 秒干净退出（实测：空闲时 0.17s；有一轮对话在飞时 ~1.05s，
# 因为那时 SIGINT 要先等 uvicorn 的优雅窗 —— 窗口由本值决定）。可用 `WEB_SHUTDOWN_GRACE_SECONDS` 覆盖。
SHUTDOWN_GRACE_SECONDS = float(os.getenv("WEB_SHUTDOWN_GRACE_SECONDS", "0.6"))


def close_log_handlers() -> None:
    """退出前**显式** flush + close 我们加的日志 handler（不靠 atexit 兜底）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        try:
            handler.flush()
            handler.close()
        except Exception:  # noqa: BLE001 —— 关日志失败不许影响退出
            pass
        finally:
            root.removeHandler(handler)


def _leftover_threads() -> list[str]:
    """后端自己起的后台线程还剩哪些（关停进度自述；`server` 还没 import 就返回空）。"""
    server_module = sys.modules.get("server")
    if server_module is None:
        return []
    try:
        return [thread.name for thread in server_module.background_threads()]
    except Exception:  # noqa: BLE001
        return []


class BoundedServer(uvicorn.Server):
    """SIGINT/SIGTERM 后**有界**退出的 uvicorn Server。

    为什么需要（根因，见 uvicorn 0.52 `uvicorn/server.py` 的 `_wait_tasks_to_complete`）：
    它的关停是"等 `server_state.connections` 与 `server_state.tasks` 自己变空"的**死循环**
    （`while ...: await asyncio.sleep(0.1)`），**只有再收到一次 Ctrl+C**（`force_exit`）才出来。
    任何一条没结束的连接/任务（观感就是"Ctrl+C 没反应，只能杀终端"）都会把它挂在那里。
    这里在**第一次**信号后就挂一个定时器：到 `grace_seconds` 还没关完就替操作者按下
    "第二次 Ctrl+C"（`force_exit = True`）—— 仍然是 uvicorn 的干净退出路径，不是 `os._exit()`。
    """

    def __init__(self, config: uvicorn.Config, grace_seconds: float):
        super().__init__(config)
        self.grace_seconds = max(0.1, float(grace_seconds))
        self._grace_timer: threading.Timer | None = None
        self._exited = threading.Event()

    def handle_exit(self, sig, frame):  # type: ignore[override]
        super().handle_exit(sig, frame)
        if self.should_exit and self._grace_timer is None:
            timer = threading.Timer(self.grace_seconds, self._force_exit)
            timer.daemon = True
            self._grace_timer = timer
            timer.start()
            logger.info("[backend] 收到信号 %s：优雅关停最多 %.1fs，到点强制收尾",
                        sig, self.grace_seconds)

    def _force_exit(self) -> None:
        if self._exited.is_set():
            return
        if not self.force_exit:
            logger.warning("[backend] 优雅关停超过 %.1fs 还没结束 ⇒ 强制收尾"
                           "（后台线程剩余：%s）", self.grace_seconds,
                           _leftover_threads() or "（无）")
        self.force_exit = True

    async def serve(self, sockets=None):  # type: ignore[override]
        try:
            return await super().serve(sockets)
        finally:
            self._exited.set()
            if self._grace_timer is not None:
                self._grace_timer.cancel()


def main(argv: list[str] | None = None) -> int:
    """起后端（uvicorn + FastAPI app）；返回进程退出码。Ctrl+C ⇒ 有界退出（≤ grace 秒）。"""
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

    config = uvicorn.Config("server:app", host=host, port=port, reload=False,
                            timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS)
    server = BoundedServer(config, grace_seconds=SHUTDOWN_GRACE_SECONDS)
    code = 0
    try:
        server.run()
    except KeyboardInterrupt:
        # uvicorn 关停完会把信号**原样再抛**（默认处理器 ⇒ KeyboardInterrupt）；那是正常退出。
        pass
    except Exception as exc:  # noqa: BLE001 —— 如实报，但不吞掉非零退出码
        logger.error("[backend] 退出前异常：%s: %s", type(exc).__name__, exc)
        code = 1
    finally:
        logger.info("[backend] 已退出（后台线程剩余：%s）", _leftover_threads() or "（无）")
        close_log_handlers()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
