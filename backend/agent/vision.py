"""视觉接入 —— 由 Python 侧直接抓屏，不经过 Java。

分层：
  ScreenCapture      抓取 Minecraft 窗口画面 -> 降采样 -> JPEG 字节
  VisionInterpreter  在后台线程低频调用 vision 角色多模态模型，把画面转成
                     一句场景描述；tick 循环只读缓存，绝不阻塞动作链路。

抓屏后端按可用性自动选择：dxcam（GPU 渲染的游戏窗口最可靠）-> mss（通用）-> 不可用。
两者都没装时整个视觉层静默降级，不影响 P0/P1/P2/P3 其它环节。
"""

import base64
import io
import logging
import sys
import threading
import time
from typing import Callable, Optional

try:
    from PIL import Image
except Exception:  # pragma: no cover - Pillow 缺失时视觉整体降级
    Image = None

logger = logging.getLogger("vision")

DEFAULT_WINDOW_TITLE = "Minecraft"
DEFAULT_MAX_WIDTH = 640
DEFAULT_JPEG_QUALITY = 70
DEFAULT_INTERVAL_SECONDS = 5.0

VISION_PROMPT = (
    "这是一张 Minecraft 游戏画面截图。请用一句中文描述当前最重要的信息："
    "玩家所处环境（地面/地下/水里/岩浆附近）、正前方有什么、附近有哪些生物或危险。"
    "只输出这一句话，不要客套，不要编造看不清的内容。"
)


def _load_backend():
    """返回 (名称, 模块)；没有任何可用抓屏后端时返回 (None, None)。"""
    try:
        import dxcam  # type: ignore
        return "dxcam", dxcam
    except Exception:
        pass
    try:
        import mss  # type: ignore
        return "mss", mss
    except Exception:
        return None, None


def _find_window_rect(title_substring: str):
    """Windows 下按标题子串查找窗口客户区，返回 (left, top, right, bottom)。

    只在 win32 上尝试；找不到窗口或非 Windows 时返回 None，调用方回退到整屏。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        target = title_substring.lower()
        found = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _visit(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if target not in buffer.value.lower():
                return True
            rect = wintypes.RECT()
            if user32.GetClientRect(hwnd, ctypes.byref(rect)):
                origin = wintypes.POINT(0, 0)
                user32.ClientToScreen(hwnd, ctypes.byref(origin))
                found.append((origin.x, origin.y,
                              origin.x + rect.right, origin.y + rect.bottom))
                return False  # 找到即停止枚举
            return True

        user32.EnumWindows(callback_type(_visit), 0)
        return found[0] if found else None
    except Exception as e:  # pragma: no cover - 依赖平台 API
        logger.debug("[Vision] 查找窗口失败: %s", e)
        return None


class ScreenCapture:
    """抓取 Minecraft 窗口画面并编码为 JPEG。

    `grabber` 可注入一个返回 PIL.Image 的可调用对象用于测试；不注入时按可用性
    自动选择 dxcam / mss 后端。
    """

    def __init__(self, window_title: str = DEFAULT_WINDOW_TITLE,
                 max_width: int = DEFAULT_MAX_WIDTH,
                 jpeg_quality: int = DEFAULT_JPEG_QUALITY,
                 grabber: Optional[Callable[[], "Image.Image"]] = None):
        self.window_title = window_title
        self.max_width = max_width
        self.jpeg_quality = jpeg_quality
        if grabber is not None:
            self.backend_name = "injected"
            self._grabber = grabber
        else:
            self.backend_name, self._grabber = self._resolve_backend()

    @property
    def available(self) -> bool:
        return self._grabber is not None and Image is not None

    def _resolve_backend(self):
        if Image is None:
            logger.warning("[Vision] 未安装 Pillow，视觉抓屏不可用")
            return None, None
        name, module = _load_backend()
        if name == "dxcam":
            rect = _find_window_rect(self.window_title)
            camera = module.create(output_idx=0, region=rect)

            def _grab():
                for _ in range(3):
                    frame = camera.grab()
                    if frame is not None:
                        return Image.fromarray(frame)
                    time.sleep(0.05)
                return None

            return name, _grab
        if name == "mss":
            rect = _find_window_rect(self.window_title)
            screen = module.mss()
            monitor = rect and {
                "left": rect[0], "top": rect[1],
                "width": rect[2] - rect[0], "height": rect[3] - rect[1],
            } or screen.monitors[1]

            def _grab():
                shot = screen.grab(monitor)
                return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

            return name, _grab
        logger.warning("[Vision] 未安装 dxcam 或 mss，视觉抓屏不可用（pip install mss）")
        return None, None

    def grab_jpeg(self) -> Optional[bytes]:
        """抓一帧并编码为 JPEG；失败返回 None。"""
        if not self.available:
            return None
        try:
            image = self._grabber()
            if image is None:
                return None
            return self._encode(image)
        except Exception as e:
            logger.warning("[Vision] 抓屏失败: %s", e)
            return None

    def _encode(self, image: "Image.Image") -> bytes:
        if image.width > self.max_width:
            ratio = self.max_width / float(image.width)
            image = image.resize((self.max_width, max(1, int(image.height * ratio))))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=self.jpeg_quality)
        return buffer.getvalue()


class VisionInterpreter:
    """低频把画面转成场景描述；tick 循环只读 `latest()`，永不阻塞。"""

    def __init__(self, llm_service, capture: Optional[ScreenCapture] = None,
                 interval: float = DEFAULT_INTERVAL_SECONDS, agent: str = "vision",
                 prompt: str = VISION_PROMPT, max_tokens: int = 160):
        self.llm = llm_service
        self.capture = capture or ScreenCapture()
        self.interval = interval
        self.agent = agent
        self.prompt = prompt
        self.max_tokens = max_tokens
        self._lock = threading.Lock()
        self._description = ""
        self._last_at = 0.0
        self._last_error = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def available(self) -> bool:
        return self.capture.available and bool(self.llm and self.llm.api_key)

    def latest(self) -> str:
        """非阻塞读取最近一次场景描述；从未成功时返回空串。"""
        with self._lock:
            return self._description

    def start(self):
        """启动后台刷新线程；不可用或已启动时不做任何事。"""
        if self._thread is not None or not self.available:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vision", daemon=True)
        self._thread.start()
        logger.info("[Vision] 视觉线程已启动（后端 %s，间隔 %.1fs）",
                    self.capture.backend_name, self.interval)
        return True

    def stop(self):
        self._stop.set()
        self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.interval)

    def refresh(self) -> Optional[str]:
        """抓一帧并调用 VLM。成功返回描述，失败返回 None 并保留上一次结果。"""
        jpeg = self.capture.grab_jpeg()
        if not jpeg:
            return None
        try:
            result = self.llm.chat_vision(self.prompt, jpeg, agent=self.agent,
                                          max_tokens=self.max_tokens)
        except Exception as e:
            with self._lock:
                self._last_error = str(e)
            logger.warning("[Vision] VLM 调用失败: %s", e)
            return None

        text = (result.get("content") or "").strip()
        if not text:
            return None
        with self._lock:
            self._description = text
            self._last_at = time.time()
            self._last_error = ""
        logger.info("[Vision] %s", text[:120])
        return text

    def get_status(self) -> dict:
        with self._lock:
            return {
                "available": self.available,
                "backend": self.capture.backend_name,
                "interval": self.interval,
                "last_at": self._last_at,
                "description": self._description,
                "error": self._last_error,
            }
