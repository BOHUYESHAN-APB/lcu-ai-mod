#!/usr/bin/env python3
"""叠层一条命令：重渲染 SVG -> 自检 alpha/几何 -> 出合成预览。

用法：
    python overlay/render.py            # 渲染 + 自检 + 出预览（推荐）
    python overlay/render.py --check    # 只对现有 PNG 自检（不重渲染）

为什么不用 ImageMagick：本机 PATH 里的 `convert` 是 Windows 自带的 FAT->NTFS 工具，
不存在 magick / inkscape / rsvg-convert / cairosvg。所以用 Chromium headless 渲染，
透明底靠 `--default-background-color=00000000`，再用 Pillow 无损重压。
"""
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 1920, 1080

# ── 几何（与 docs/dev 的设计及 overlay/README.md 一致；改布局先改这里）────────────
# 用户要的层次：**右下角 POV 是个完整的长方框（四条边都在，画在最上层）；
# 左上大框被它压着，所以大框在交叠区是残缺的（大框的边不能画到小框里）**。
GEOM = {
    "ai_inner": (48, 76, 1312, 738),        # AI 视角内屏（画面源位置）
    "pov_inner": (1144, 619, 720, 405),     # 用户 POV 内屏（放大档，压住 AI 屏右下角）
    "pov_outer": (1100, 575, 808, 493),     # POV 外壳（内屏外扩 44px），四边必须完整
    "right_col": (1400, 76, 500, 470),      # 右侧蓝列（底图，纯色；下沿 546，避开 POV 外壳）
    "bottom_bar": (48, 1024, 1040, 40),     # 底边蓝条（底图，纯色；右端 1088，避开 POV 外壳）
}
# POV 压住 AI 屏的重叠带（用户要的"小框压大框"）
OVERLAP = (1144, 619, 1360, 814)
# alpha 门槛：低于它的像素直接清成 0，高于它的实心化 —— 叠层必须"要么全透要么全不透"，
# 否则开孔边缘的半透明像素叠在游戏画面上会出现一层脏边（用户实测报的 bug）。
ALPHA_SNAP = 128

CHROME_CANDIDATES = [
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Microsoft/Edge/Application/msedge.exe",
]


def find_chrome():
    for path in CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    raise SystemExit("找不到 Chrome/Edge，无法渲染（装了再来，或手动跑 README 里的命令）")


def snap_alpha(img):
    """把 alpha 二值化：>=128 -> 255，否则 0。返回 (图像, 被改动的像素数)。

    半透明像素在 OBS 叠加时会和视频混合出一层肉眼可见的脏边/晕，透明叠层应该只有
    "开孔(0)" 和 "实心(255)" 两种状态。
    """
    arr = np.array(img)
    alpha = arr[:, :, 3]
    changed = int(((alpha != 0) & (alpha != 255)).sum())
    arr[:, :, 3] = np.where(alpha >= ALPHA_SNAP, 255, 0).astype(np.uint8)
    return Image.fromarray(arr, "RGBA"), changed


def render(svg_name, out_name):
    svg = os.path.join(HERE, svg_name)
    out = os.path.join(HERE, out_name)
    if not os.path.exists(svg):
        raise SystemExit(f"缺 SVG 源文件：{svg}")
    with tempfile.TemporaryDirectory(prefix="lcu_ov_") as tmp:
        cmd = [
            find_chrome(),
            "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--hide-scrollbars", "--force-device-scale-factor=1",
            f"--window-size={W},{H}", "--default-background-color=00000000",
            "--virtual-time-budget=4000", f"--user-data-dir={tmp}",
            f"--screenshot={out}", "file:///" + svg.replace("\\", "/"),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    img = Image.open(out).convert("RGBA")
    if img.size != (W, H):
        raise SystemExit(f"渲染尺寸不对：{img.size} != {(W, H)}")
    img, changed = snap_alpha(img)
    print(f"{out_name}: alpha 二值化，修正 {changed} 个半透明像素")
    # 无损重压（Chrome 的 PNG 压缩率一般）
    img.save(out, optimize=True, compress_level=9)
    return out


def alpha_stats(arr, rect):
    x, y, w, h = rect
    region = arr[y:y + h, x:x + w, 3]
    return int(region.max()), int((region > 0).sum())


def check():
    ok = True
    base = np.array(Image.open(os.path.join(HERE, "lcumod_overlay_base.png")).convert("RGBA"))
    top = np.array(Image.open(os.path.join(HERE, "lcumod_overlay_top.png")).convert("RGBA"))

    # ① 底图：POV 矩形内必须全透明
    mx, nz = alpha_stats(base, GEOM["pov_inner"])
    good = mx == 0 and nz == 0
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  base POV 区全透明: alpha_max={mx} nonzero={nz}")

    # ② 底图：POV 之外必须完全不透明（整屏铺满，不能漏游戏画面）
    mask = np.ones((H, W), dtype=bool)
    x, y, w, h = GEOM["pov_inner"]
    mask[y:y + h, x:x + w] = False
    outside_min = int(base[:, :, 3][mask].min())
    good = outside_min == 255
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  base POV 之外不透明: 最小 alpha={outside_min}（要求 255）")

    # ③ 顶图开孔
    #    - POV 内屏：必须整块透明
    #    - AI 内屏：整块透明**除了被 POV 外壳压住的那块** —— 用户明确要求"右下小框是完整的长方框，
    #      它压着大框"，而顶图叠在视频源之上，所以小框边框压在 AI 画面上是对的（不能为了开孔把边框切掉）。
    mx, nz = alpha_stats(top, GEOM["pov_inner"])
    good = mx == 0 and nz == 0
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  top pov_inner 开孔: alpha_max={mx} nonzero={nz}")

    ax, ay, aw, ah = GEOM["ai_inner"]
    sub = top[ay:ay + ah, ax:ax + aw, 3].copy()
    ox, oy, ow, oh = GEOM["pov_outer"]
    ix0, iy0 = max(ax, ox), max(ay, oy)
    ix1, iy1 = min(ax + aw, ox + ow), min(ay + ah, oy + oh)
    covered = 0
    if ix1 > ix0 and iy1 > iy0:
        covered = (ix1 - ix0) * (iy1 - iy0)
        sub[iy0 - ay:iy1 - ay, ix0 - ax:ix1 - ax] = 0
    mx, nz = int(sub.max()), int((sub > 0).sum())
    good = mx == 0 and nz == 0
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  top ai_inner 开孔（POV 压住的 {covered}px 除外）: alpha_max={mx} nonzero={nz}")

    # ③b 小框的四条边**即使在 AI 开孔里也必须实心**（"完整的长方框"的关键：它是画在最上层的）
    probes = [((ix0 + ix1) // 2, oy + 22), (ox + 22, (iy0 + iy1) // 2)]
    vals2 = [int(top[py, px, 3]) for px, py in probes]
    good = min(vals2) == 255
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  小框边框压在 AI 画面上仍实心: {probes} -> alpha={vals2}")

    # ④ 顶图外壳要不透明（不然边框是空的）
    sx, sy, sw, sh = GEOM["ai_inner"]
    shell_pts = [(sx - 10, sy + sh // 2), (sx + sw // 2, sy - 10), (sx + sw + 10, sy + sh // 2),
                 (sx + sw // 2, sy + sh + 10), (sx - 10, sy - 10)]
    shell_min = min(int(top[py, px, 3]) for px, py in shell_pts)
    good = shell_min == 255
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  top AI 外壳不透明: 最小 alpha={shell_min}（要求 255）")

    # ④b 小框（POV）四条边必须都在（不能被画布边缘切掉、不能被大框盖掉）：
    #     取外壳四条边的中点，必须是不透明的边框实体
    ox, oy, ow, oh = GEOM["pov_outer"]
    if ox < 0 or oy < 0 or ox + ow > W or oy + oh > H:
        ok = False
        print(f"FAIL  POV 外壳超出画布: {GEOM['pov_outer']}（会被边缘切掉，小框就不完整了）")
    edge_pts = [(ox + ow // 2, oy + 22), (ox + ow // 2, oy + oh - 22),
                (ox + 22, oy + oh // 2), (ox + ow - 22, oy + oh // 2)]
    vals = [int(top[py, px, 3]) for px, py in edge_pts]
    good = min(vals) == 255
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  POV 外壳四边完整: 边中点 alpha={vals}（要求全 255）")

    # ⑤ 两图尺寸/RGBA + alpha 必须只有 0/255
    for label, img in (("base", base), ("top", top)):
        good = img.shape == (H, W, 4)
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  {label} 尺寸: {img.shape[1]}x{img.shape[0]} RGBA={img.shape[2] == 4}")
        semis = int(((img[:, :, 3] > 0) & (img[:, :, 3] < 255)).sum())
        good = semis == 0
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  {label} alpha 二值: 半透明像素={semis}（要求 0）")

    return ok


def composite():
    """合成预览：底图 + 假视频画面 + 顶图；并断言重叠带确实是 POV 压在 AI 屏上。"""
    base = Image.open(os.path.join(HERE, "lcumod_overlay_base.png")).convert("RGBA")
    top = Image.open(os.path.join(HERE, "lcumod_overlay_top.png")).convert("RGBA")
    prev = base.copy()

    AI_PLACEHOLDER = (24, 52, 84, 255)
    POV_PLACEHOLDER = (92, 60, 34, 255)
    for rect, color, label in ((GEOM["ai_inner"], AI_PLACEHOLDER, "AI VIEW"),
                               (GEOM["pov_inner"], POV_PLACEHOLDER, "USER POV")):
        x, y, w, h = rect
        board = Image.new("RGBA", (w, h), color)
        d = ImageDraw.Draw(board)
        d.rectangle([0, 0, w - 1, h - 1], outline=(200, 240, 255, 255), width=2)
        d.text((16, 14), f"{label}  {w}x{h}", fill=(235, 250, 255, 255))
        prev.alpha_composite(board, (x, y))

    out = Image.alpha_composite(prev, top).convert("RGB")
    out.save(os.path.join(HERE, "preview_composite.png"), optimize=True)

    arr = np.array(out)
    ox, oy, _, _ = OVERLAP
    cx, cy = ox + 20, oy + 20
    got = tuple(int(v) for v in arr[cy, cx])
    want = POV_PLACEHOLDER[:3]
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  重叠带 ({cx},{cy}) 应为 POV 画面 {want}，实测 {got}")
    print("已写 overlay/preview_composite.png")
    return ok


def main():
    if "--check" not in sys.argv:
        render("lcumod_overlay_base.svg", "lcumod_overlay_base.png")
        render("lcumod_overlay_top.svg", "lcumod_overlay_top.png")
    ok = check()
    ok &= composite()
    print("\n结果:", "ALL PASS" if ok else "有 FAIL —— 看上面的行")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
