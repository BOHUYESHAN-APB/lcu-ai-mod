"""蓝图只读解析层 —— 读 Litematica / Sponge 蓝图文件，产出结构化清单 + 人读摘要。

用户口径：「让 AI 照着蓝图建造」，而且「大家都能看到蓝图」。
这一层**只做文件解析**，不依赖任何第三方模组、不依赖任何 pip 包（只用标准库）：

    gzip/zlib 解压  →  最小 NBT 读取  →  位打包/varint 解码  →  聚合材料清单  →  人读摘要

支持的容器格式（版本不认就**如实拒绝**，绝不猜格式）：

    .litematic  Litematica（palette + 位打包 long[] + 多 region；BlockStates 为**紧致打包**，
                条目**可以跨 long 边界**——见 `_get_at_tight`）
    .nbt/.schem Sponge Schematic v2（Palette/BlockData 在根；Offset 是世界原点相对）
                Sponge Schematic v3（Blocks 复合标签内放 Palette/Data；Offset 直接可用）

明确**不支持**：legacy MCEdit `.schematic`（`Blocks` 是 byte[]、`Materials` 文本表）——
识别得到但报错拒绝（`code="legacy_schematic"`），不做猜测式兼容。

内存纪律（本仓「上下文/内存加界」的要求）：
  - 输入文件上限（默认 64 MiB）、解压后上限（默认 256 MiB）、region 数上限、总格子数上限；
  - 逐块读取是**生成器**（`Blueprint.iter_blocks` / `Region.iter_local`），
    绝不为几百万方块建 Python 对象列表；材料聚合只是一张 palette 大小的计数表。

给模型的渲染在 `render_summary(bp, lang="zh"|"en")`：只给尺寸/总格数/前 N 项材料，
超长就提示「还有 X 种，用 blueprint_materials 查全部」，绝不把逐块数据塞进上下文。
分片查询用 `Blueprint.iter_blocks(region=…, y_range=…, box=…)` 一块一块问。

自检：`backend/tests/test_blueprint.py`（用自造的最小 NBT 字节当夹具）。
**未与真实 .litematic 文件对账**：位打包是按 Litematica 的 `LitematicaBitArray` 语义
（条目跨 long、长度 = roundUp(n*bits, 64)/64）实现的，自造夹具能覆盖跨边界，但真实文件仍需对账。
"""

from __future__ import annotations

import os
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "BlueprintError",
    "Blueprint",
    "Region",
    "Material",
    "load_blueprint",
    "parse_blueprint_bytes",
    "render_summary",
    "TOOL_HELP",
    "SUPPORTED_SPONGE_VERSIONS",
    "SUPPORTED_LITEMATIC_FORMAT_VERSIONS",
    "SUPPORTED_DATA_VERSION_RANGE",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_DECOMPRESSED_BYTES",
    "DEFAULT_MAX_REGIONS",
    "DEFAULT_MAX_BLOCKS",
]


# ── 上限（全部可覆盖；测试用小的值就能触发，不必造 64MB 假文件）──────
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024          # 输入文件（压缩态）上限
DEFAULT_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024  # 解压后上限（防 gzip 炸弹）
DEFAULT_MAX_REGIONS = 4096
DEFAULT_MAX_BLOCKS = 32_000_000                    # 所有 region 格子数之和的上限

# ── 支持的版本窗口（不认就拒绝；错误信息里会带上"我认识的范围 + 文件里的值"）──
# Sponge：格式版本只认 2 与 3（v1 是 MCEdit 血统，`Blocks` 为 byte[]，另行列拒）。
SUPPORTED_SPONGE_VERSIONS: tuple[int, ...] = (2, 3)
# Litematica：格式版本 1..7 的容器布局一致（Regions/Position/Size/BlockStatePalette/BlockStates）。
SUPPORTED_LITEMATIC_FORMAT_VERSIONS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7)
# 游戏数据版本：1451 = 1.13「扁平化」起点（此前的数值 id 世界格式本层不支持）。
# 上限只是"明显不是版本号"的护栏 —— 调色板条目是带命名空间的字符串，容器布局不随它变化。
SUPPORTED_DATA_VERSION_RANGE: tuple[int, int] = (1451, 100_000)

_MAX_NBT_DEPTH = 128


class BlueprintError(ValueError):
    """蓝图解析失败（**如实拒绝**，绝不假装成功）。

    `code` 供上层分支：not_found / too_large / bad_compression / not_nbt /
    unsupported_format / legacy_schematic / unsupported_version / missing_field /
    bad_field / bad_palette / truncated / too_many_regions / too_many_blocks。
    """

    def __init__(self, message: str, *, code: str = "blueprint_error",
                 detail: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = dict(detail or {})

    def __str__(self) -> str:  # pragma: no cover - 只是让日志好看
        return str(self.args[0]) if self.args else self.code


# ══════════════════════════════════════════════════════════════════════
# 1. 最小 NBT 读取（标准库；标签类型见 Minecraft NBT 规范）
# ══════════════════════════════════════════════════════════════════════

_TAG_END, _TAG_BYTE, _TAG_SHORT, _TAG_INT, _TAG_LONG = 0, 1, 2, 3, 4
_TAG_FLOAT, _TAG_DOUBLE, _TAG_BYTE_ARRAY, _TAG_STRING = 5, 6, 7, 8
_TAG_LIST, _TAG_COMPOUND, _TAG_INT_ARRAY, _TAG_LONG_ARRAY = 9, 10, 11, 12


class ByteArray(bytes):
    """NBT `TAG_Byte_Array`（与普通 `bytes` 区分，用于识别 legacy `.schematic`）。"""


class IntArray(list):
    """NBT `TAG_Int_Array`。"""


class LongArray(list):
    """NBT `TAG_Long_Array`。"""


class _Reader:
    """大端 NBT 读取器；越界一律抛 `truncated`（不静默补零）。"""

    __slots__ = ("buf", "pos", "source")

    def __init__(self, buf: bytes, source: str) -> None:
        self.buf = buf
        self.pos = 0
        self.source = source

    def _take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.buf):
            raise BlueprintError(
                f"{self.source}: NBT 数据在偏移 {self.pos} 处截断（还要 {n} 字节，"
                f"只剩 {len(self.buf) - self.pos}）",
                code="truncated")
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def u1(self) -> int:
        return self._take(1)[0]

    def i2(self) -> int:
        return int.from_bytes(self._take(2), "big", signed=True)

    def i4(self) -> int:
        return int.from_bytes(self._take(4), "big", signed=True)

    def i8(self) -> int:
        return int.from_bytes(self._take(8), "big", signed=True)

    def f4(self) -> float:
        import struct
        return struct.unpack(">f", self._take(4))[0]

    def f8(self) -> float:
        import struct
        return struct.unpack(">d", self._take(8))[0]

    def string(self) -> str:
        n = int.from_bytes(self._take(2), "big", signed=False)
        return self._take(n).decode("utf-8", errors="replace")

    def payload(self, tag: int, depth: int = 0) -> Any:
        if depth > _MAX_NBT_DEPTH:
            raise BlueprintError(f"{self.source}: NBT 嵌套过深（>{_MAX_NBT_DEPTH}）",
                                 code="bad_field")
        if tag == _TAG_BYTE:
            return int.from_bytes(self._take(1), "big", signed=True)
        if tag == _TAG_SHORT:
            return self.i2()
        if tag == _TAG_INT:
            return self.i4()
        if tag == _TAG_LONG:
            return self.i8()
        if tag == _TAG_FLOAT:
            return self.f4()
        if tag == _TAG_DOUBLE:
            return self.f8()
        if tag == _TAG_BYTE_ARRAY:
            n = self.i4()
            if n < 0:
                raise BlueprintError(f"{self.source}: BYTE_ARRAY 长度为负（{n}）",
                                     code="bad_field")
            return ByteArray(self._take(n))
        if tag == _TAG_STRING:
            return self.string()
        if tag == _TAG_LIST:
            elem = self.u1()
            n = self.i4()
            if n < 0:
                raise BlueprintError(f"{self.source}: LIST 长度为负（{n}）", code="bad_field")
            if elem == _TAG_END:
                return [] if n == 0 else _bad_list(self.source, n)
            # 每个元素至少 1 字节 ⇒ 长度不能超过剩余字节数（防伪造长度把内存打爆）
            if n > len(self.buf) - self.pos + 1:
                raise BlueprintError(
                    f"{self.source}: LIST 声明 {n} 个元素但剩余数据不足", code="truncated")
            return [self.payload(elem, depth + 1) for _ in range(n)]
        if tag == _TAG_COMPOUND:
            out: dict[str, Any] = {}
            while True:
                child = self.u1()
                if child == _TAG_END:
                    return out
                name = self.string()
                out[name] = self.payload(child, depth + 1)
        if tag == _TAG_INT_ARRAY:
            n = self.i4()
            if n < 0:
                raise BlueprintError(f"{self.source}: INT_ARRAY 长度为负（{n}）",
                                     code="bad_field")
            self._check_array(n, 4)
            return IntArray(self.i4() for _ in range(n))
        if tag == _TAG_LONG_ARRAY:
            n = self.i4()
            if n < 0:
                raise BlueprintError(f"{self.source}: LONG_ARRAY 长度为负（{n}）",
                                     code="bad_field")
            self._check_array(n, 8)
            return LongArray(self.i8() for _ in range(n))
        raise BlueprintError(f"{self.source}: 未知 NBT 标签类型 {tag}", code="not_nbt")

    def _check_array(self, n: int, width: int) -> None:
        if n * width > len(self.buf) - self.pos:
            raise BlueprintError(
                f"{self.source}: 数组声明 {n} 项（{width} 字节/项）但剩余数据不足",
                code="truncated")


def _bad_list(source: str, n: int) -> Any:
    raise BlueprintError(f"{source}: LIST 元素类型为 END 但长度 {n} != 0", code="bad_field")


def _decompress(raw: bytes, *, max_bytes: int, source: str) -> bytes:
    """按魔数解压（gzip / zlib / 原样）；解压有上限，防炸弹。"""
    if raw[:2] == b"\x1f\x8b":
        wbits = 16 + zlib.MAX_WBITS
    elif raw[:1] == b"\x78":
        wbits = zlib.MAX_WBITS
    else:
        return raw
    obj = zlib.decompressobj(wbits)
    out = bytearray()
    pending = raw
    try:
        while True:
            out += obj.decompress(pending, 1 << 20)
            if len(out) > max_bytes:
                raise BlueprintError(
                    f"{source}: 解压后超过上限 {max_bytes} 字节（疑似压缩炸弹）",
                    code="too_large", detail={"limit": max_bytes})
            pending = obj.unconsumed_tail
            if not pending:
                break
        out += obj.flush()
    except zlib.error as exc:
        raise BlueprintError(f"{source}: 解压失败（{exc}）", code="bad_compression") from exc
    if len(out) > max_bytes:
        raise BlueprintError(f"{source}: 解压后超过上限 {max_bytes} 字节",
                             code="too_large", detail={"limit": max_bytes})
    if not obj.eof:
        # 未读到流尾 = 尾巴被截掉。如实报错，不去猜剩下的字节。
        raise BlueprintError(f"{source}: 压缩流不完整（未读到流尾，疑似文件被截断）",
                             code="bad_compression")
    return bytes(out)


def _read_root_nbt(raw: bytes, *, source: str, max_decompressed: int) -> dict[str, Any]:
    """解压 + 读根复合标签。"""
    if raw[:2] == b"\x1f\x8b" or raw[:1] == b"\x78":
        try:
            buf = _decompress(raw, max_bytes=max_decompressed, source=source)
        except BlueprintError:
            raise
        except Exception as exc:
            raise BlueprintError(f"{source}: 解压失败（{exc}）", code="bad_compression") from exc
    else:
        buf = raw
    if not buf:
        raise BlueprintError(f"{source}: 文件为空", code="not_nbt")
    reader = _Reader(buf, source)
    tag = reader.u1()
    # 允许无名的根复合标签（少数写入器省略根名）
    if tag != _TAG_COMPOUND:
        raise BlueprintError(
            f"{source}: 根标签是 {tag}（应为 COMPOUND=10）—— 不是 NBT 蓝图",
            code="not_nbt")
    reader.string()  # 根名（通常为空串）
    return reader.payload(_TAG_COMPOUND)


# ══════════════════════════════════════════════════════════════════════
# 2. 方块分类（哪些"物化不了 / 需要确认"）
# ══════════════════════════════════════════════════════════════════════

# 判据（都是"这个方块在物品栏里没有对应物品"）：
#   - 空气（含 `#minecraft:air` 标签的 cave_air / void_air）—— 不是方块物品；
#   - 流体（water / lava 及 flowed 变体）—— 只能用水桶类物品，且放置语义不同；
#   - 技术方块（structure_void / moving_piston / piston_head / bubble_column / fire 等）
#     —— 由游戏运行时产生，物品栏里没有；
#   - 仅创造可获得 / 生存拿不到（bedrock / command_block / barrier / 传送门方块…）。
# ⚠ 这是**保守枚举**（依据是常见版本的经验集合，不是从本仓数据推导）：
#   没列到的按"可获取"处理；拿不准的第三方模组方块单独进 `unverified`，不混进"能拿到"。
NON_ITEM_BLOCKS: frozenset[str] = frozenset({
    "minecraft:air", "minecraft:cave_air", "minecraft:void_air",
    "minecraft:water", "minecraft:lava",
    "minecraft:fire", "minecraft:soul_fire",
    "minecraft:structure_void", "minecraft:moving_piston", "minecraft:piston_head",
    "minecraft:bubble_column", "minecraft:end_portal", "minecraft:nether_portal",
    "minecraft:end_gateway", "minecraft:light",
})

CREATIVE_ONLY_BLOCKS: frozenset[str] = frozenset({
    "minecraft:bedrock", "minecraft:barrier", "minecraft:command_block",
    "minecraft:chain_command_block", "minecraft:repeating_command_block",
    "minecraft:structure_block", "minecraft:jigsaw", "minecraft:spawner",
    "minecraft:trial_spawner", "minecraft:vault", "minecraft:budding_amethyst",
    "minecraft:reinforced_deepslate", "minecraft:petrified_oak_slab",
    "minecraft:end_portal_frame", "minecraft:debug_stick",
})

# 有方块、但物品栏里没有"直接可放的那件物品"（要经一步转换/加工）。
NO_DIRECT_ITEM_BLOCKS: frozenset[str] = frozenset({
    "minecraft:farmland", "minecraft:dirt_path",
    "minecraft:water_cauldron", "minecraft:lava_cauldron", "minecraft:powder_snow_cauldron",
    "minecraft:wheat", "minecraft:carrots", "minecraft:potatoes", "minecraft:beetroots",
    "minecraft:torchflower_crop", "minecraft:pitcher_crop",
    "minecraft:melon_stem", "minecraft:pumpkin_stem",
    "minecraft:attached_melon_stem", "minecraft:attached_pumpkin_stem",
    "minecraft:big_dripleaf_stem", "minecraft:cocoa", "minecraft:sweet_berry_bush",
    "minecraft:kelp_plant", "minecraft:twisting_vines_plant", "minecraft:weeping_vines_plant",
    "minecraft:cave_vines_plant", "minecraft:frosted_ice", "minecraft:frogspawn",
})
_NO_DIRECT_ITEM_PREFIXES: tuple[str, ...] = ("minecraft:potted_", "minecraft:infested_")
_NO_DIRECT_ITEM_SUFFIXES: tuple[str, ...] = ("_stem", "_crop")

# 空气（不计入材料清单；但计入"占位/尺寸"）
AIR_BLOCKS: frozenset[str] = frozenset({
    "minecraft:air", "minecraft:cave_air", "minecraft:void_air"})

_FLUID_BLOCKS: frozenset[str] = frozenset({"minecraft:water", "minecraft:lava"})


def _split_block(block: str) -> tuple[str, str, str]:
    """`"minecraft:oak_stairs[facing=east]"` → `("minecraft", "oak_stairs", "oak_stairs")`。"""
    base = block.split("[", 1)[0].strip()
    if ":" in base:
        ns, _, name = base.partition(":")
    else:
        ns, name = "minecraft", base
    return ns, name, base


def _classify(block: str) -> Optional[tuple[str, str]]:
    """返回 `(code, note)`；`None` ⇒ 这是一件"能照着盖"的普通方块。

    code ∈ not_an_item / creative_only / no_direct_item / fluid / unverified。
    """
    ns, _name, base = _split_block(block)
    if base in AIR_BLOCKS:
        return "not_an_item", "空气（不是物品，不用准备）"
    if base in _FLUID_BLOCKS:
        return "fluid", "流体（不是物品；只能用水桶/岩浆桶另作处理）"
    if base in NON_ITEM_BLOCKS:
        return "not_an_item", "技术方块／运行时方块（物品栏里没有对应物品）"
    if ns != "minecraft":
        return "unverified", "第三方模组方块 —— 有没有对应物品、能不能拿到我无法确认，需人工确认"
    if base in CREATIVE_ONLY_BLOCKS:
        return "creative_only", "仅创造可获得（生存拿不到）"
    if base in NO_DIRECT_ITEM_BLOCKS:
        return "no_direct_item", "没有直接可放的物品（要先耕地/播种/装桶等一步转换）"
    if any(base.startswith(p) for p in _NO_DIRECT_ITEM_PREFIXES):
        return "no_direct_item", "没有直接可放的物品（花盆/虫蚀类需先放底座）"
    if any(base.endswith(s) for s in _NO_DIRECT_ITEM_SUFFIXES):
        return "no_direct_item", "没有直接可放的物品（茎／作物需种子）"
    return None


# ══════════════════════════════════════════════════════════════════════
# 3. 数据结构
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Material:
    """一种方块及其数量。`tags` 是它命中的问题（空集 ⇒ 正常可盖）。"""

    block: str
    count: int
    tags: frozenset[str] = frozenset()
    notes: tuple[str, ...] = ()

    @property
    def placeable(self) -> bool:
        return not self.tags

    @property
    def needs_property(self) -> bool:
        return "needs_property" in self.tags

    def to_dict(self) -> dict:
        return {"block": self.block, "count": self.count,
                "tags": sorted(self.tags), "notes": list(self.notes)}


@dataclass
class Region:
    """一个子区域：原点是**世界坐标**（可为负），尺寸可带符号（Litematica 允许）。

    本地坐标永远是 `0..abs(size)-1`；世界坐标 = `origin + 本地坐标`。
    `states` 保留原始 long[]（不展开成块列表）；`iter_local()` 按需解码。
    """

    name: str
    origin: tuple[int, int, int]
    size: tuple[int, int, int]
    palette: tuple[str, ...]
    bits: int
    states: LongArray
    block_entities: tuple[dict, ...] = ()
    entities: tuple[dict, ...] = ()

    def __post_init__(self) -> None:
        self.abs_size = (abs(int(self.size[0])), abs(int(self.size[1])), abs(int(self.size[2])))

    @property
    def volume(self) -> int:
        ax, ay, az = self.abs_size
        return ax * ay * az

    def index_of(self, lx: int, ly: int, lz: int) -> int:
        ax, _ay, az = self.abs_size
        return (ly * az + lz) * ax + lx

    def palette_index(self, index: int) -> int:
        """紧致位打包（**条目可跨 long 边界**，按 Litematica `LitematicaBitArray` 语义）。"""
        states, bits = self.states, self.bits
        start_offset = index * bits
        start = start_offset >> 6
        end = ((index + 1) * bits - 1) >> 6
        bit = start_offset & 0x3F
        if start >= len(states) or end >= len(states):
            raise BlueprintError(
                f"region {self.name!r}: BlockStates 长度不足（索引 {index} 需要 long[{end}]，"
                f"实际只有 {len(states)} 个）", code="truncated")
        mask = (1 << bits) - 1
        a = states[start] & 0xFFFFFFFFFFFFFFFF
        if start == end:
            return (a >> bit) & mask
        b = states[end] & 0xFFFFFFFFFFFFFFFF
        return ((a >> bit) | (b << (64 - bit))) & mask

    def block_at_local(self, lx: int, ly: int, lz: int) -> str:
        pid = self.palette_index(self.index_of(lx, ly, lz))
        if pid < 0 or pid >= len(self.palette):
            raise BlueprintError(
                f"region {self.name!r}: 调色板索引 {pid} 越界（调色板只有 {len(self.palette)} 项）",
                code="bad_palette")
        block = self.palette[pid]
        if block is _HOLE:
            raise BlueprintError(
                f"region {self.name!r}: 调色板索引 {pid} 是空洞（调色板索引不连续），"
                "不猜它是什么方块", code="bad_palette")
        return block

    def world_of(self, lx: int, ly: int, lz: int) -> tuple[int, int, int]:
        ox, oy, oz = self.origin
        return (ox + lx, oy + ly, oz + lz)

    def iter_local(self, *, y_range: Optional[tuple[int, int]] = None,
                   box: Optional[tuple[int, int, int, int, int, int]] = None,
                   include_air: bool = True, limit: Optional[int] = None
                   ) -> Iterator[tuple[int, int, int, str]]:
        """流式逐块（本地坐标 → 方块）。**不建列表**，一次只产一个。

        `y_range` 是**世界 Y** 范围 `[起, 止)`（止不含）—— 按建造层分片用；
        `box` 也是世界坐标（min 含 / max 不含）。
        """
        ax, ay, az = self.abs_size
        x0, x1, z0, z1 = 0, ax, 0, az
        y0, y1 = 0, ay
        ox, oy, oz = self.origin
        if y_range is not None:
            y0 = max(0, int(y_range[0]) - oy)
            y1 = min(ay, int(y_range[1]) - oy)
        if box is not None:
            bx0, by0, bz0, bx1, by1, bz1 = (int(v) for v in box)
            x0 = max(x0, bx0 - ox); x1 = min(x1, bx1 - ox)
            y0 = max(y0, by0 - oy); y1 = min(y1, by1 - oy)
            z0 = max(z0, bz0 - oz); z1 = min(z1, bz1 - oz)
        emitted = 0
        for ly in range(y0, y1):
            for lz in range(z0, z1):
                for lx in range(x0, x1):
                    block = self.block_at_local(lx, ly, lz)
                    if not include_air and block.split("[", 1)[0] in AIR_BLOCKS:
                        continue
                    yield (lx, ly, lz, block)
                    emitted += 1
                    if limit is not None and emitted >= limit:
                        return

    def iter_blocks(self, **kw) -> Iterator[tuple[int, int, int, str]]:
        for lx, ly, lz, block in self.iter_local(**kw):
            yield (*self.world_of(lx, ly, lz), block)


@dataclass
class Blueprint:
    """解析后的蓝图（结构化）。字段名稳定，供工具层/模型摘要用。"""

    format: str                       # "litematic" | "sponge"
    format_version: int
    data_version: int
    size: tuple[int, int, int]        # 包围盒尺寸（各轴 ≥0）
    origin: tuple[int, int, int]      # 包围盒最小角（世界坐标；可为负）
    regions: tuple[Region, ...]
    materials: tuple[Material, ...] = ()
    excluded_materials: tuple[Material, ...] = ()
    block_entities: tuple[dict, ...] = ()
    entities: tuple[dict, ...] = ()
    name: Optional[str] = None
    author: Optional[str] = None
    description: Optional[str] = None
    layout: str = ""
    notes: tuple[str, ...] = ()
    source: str = ""

    # ── 便捷视图（都是 materials 的切片，不重算）──
    @property
    def total_volume(self) -> int:
        return sum(r.volume for r in self.regions)

    @property
    def total_blocks(self) -> int:
        """非空气格子数（材料清单各项之和）。"""
        return sum(m.count for m in self.materials)

    @property
    def air_blocks(self) -> int:
        return self.total_volume - self.total_blocks

    @property
    def placeable_materials(self) -> tuple[Material, ...]:
        """真正要备料的（能照着盖、方块类型对得上）。"""
        return tuple(m for m in self.materials if m.placeable)

    @property
    def needs_property(self) -> tuple[Material, ...]:
        return tuple(m for m in self.materials if m.needs_property)

    @property
    def not_placeable(self) -> tuple[Material, ...]:
        """物化不了 / 需要确认：非物品（空气/流体/技术方块）、仅创造、无直接物品。"""
        bad = {"not_an_item", "fluid", "creative_only", "no_direct_item"}
        merged = tuple(m for m in self.materials if m.tags & bad) + tuple(self.excluded_materials)
        return tuple(sorted(merged, key=lambda m: (-m.count, m.block)))

    @property
    def unverified(self) -> tuple[Material, ...]:
        return tuple(m for m in self.materials if "unverified" in m.tags)

    @property
    def region_names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.regions)

    # ── 分片查询 ──
    def iter_blocks(self, *, region: Optional[str] = None,
                    y_range: Optional[tuple[int, int]] = None,
                    box: Optional[tuple[int, int, int, int, int, int]] = None,
                    include_air: bool = True, limit: Optional[int] = None
                    ) -> Iterator[tuple[int, int, int, str]]:
        """流式逐块（**世界坐标**）。`region`/`y_range`/`box` 任选其一或组合。

        `box=(x0,y0,z0,x1,y1,z1)`：世界坐标，min 含、max 不含。
        """
        emitted = 0
        for reg in self.regions:
            if region is not None and reg.name != region:
                continue
            for x, y, z, block in reg.iter_blocks(y_range=y_range, box=box,
                                                 include_air=include_air):
                yield (x, y, z, block)
                emitted += 1
                if limit is not None and emitted >= limit:
                    return

    def materials_page(self, offset: int = 0, limit: int = 50) -> list[dict]:
        """材料清单分页（超长清单别一次全给模型）。"""
        rows = self.materials[max(0, offset):max(0, offset) + max(0, limit)]
        return [m.to_dict() for m in rows]


# ══════════════════════════════════════════════════════════════════════
# 4. 聚合 / 校验工具
# ══════════════════════════════════════════════════════════════════════


def _aggregate(regions: Sequence[Region]) -> tuple[tuple[Material, ...], tuple[Material, ...]]:
    """逐块流式计数（只留一张 palette 大小的计数表，绝不建块列表）。

    返回 `(materials, excluded)`：`materials` 是"要准备的材料"（**空气不计**），
    `excluded` 是被剔出去的空气类（数量照算，单独列出 —— 空气不是物品，但占位/尺寸照算）。
    """
    counts: dict[str, int] = {}
    air: dict[str, int] = {}
    for reg in regions:
        ax, ay, az = reg.abs_size
        if not (ax and ay and az):
            continue
        for _lx, _ly, _lz, block in reg.iter_local():
            if block.split("[", 1)[0] in AIR_BLOCKS:
                air[block] = air.get(block, 0) + 1
                continue
            counts[block] = counts.get(block, 0) + 1
    out: list[Material] = []
    for block, count in counts.items():
        tags: set[str] = set()
        notes: list[str] = []
        verdict = _classify(block)
        if verdict is not None:
            tags.add(verdict[0])
            notes.append(verdict[1])
        if "[" in block:
            tags.add("needs_property")
            notes.append("带方块状态属性（楼梯朝向／原木轴向／观察者朝向…）；"
                         "当前放置路径只按**方块类型**核对"
                         "（ActionExecutor.isAlreadyBlock: getBlockState(pos).is(...)），"
                         "盖出来朝向可能不对")
        out.append(Material(block, count, frozenset(tags), tuple(notes)))
    out.sort(key=lambda m: (-m.count, m.block))
    excluded = tuple(
        Material(block, count, frozenset({_classify(block)[0] if _classify(block) else "not_an_item"}),
                 (_classify(block)[1] if _classify(block) else "不是方块物品（不准备材料）"))
        for block, count in sorted(air.items(), key=lambda kv: (-kv[1], kv[0])))
    return tuple(out), excluded


def _bbox(regions: Sequence[Region]) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """所有 region 的包围盒（(min), (size)）。"""
    if not regions:
        raise BlueprintError("蓝图里没有任何 region", code="missing_field")
    mins = [0, 0, 0]
    maxs = [0, 0, 0]
    for i, reg in enumerate(regions):
        ox, oy, oz = reg.origin
        sx, sy, sz = reg.size
        lo = (min(ox, ox + sx), min(oy, oy + sy), min(oz, oz + sz))
        hi = (max(ox, ox + sx), max(oy, oy + sy), max(oz, oz + sz))
        if i == 0:
            mins, maxs = list(lo), list(hi)
        else:
            for k in range(3):
                mins[k] = min(mins[k], lo[k])
                maxs[k] = max(maxs[k], hi[k])
    origin = (mins[0], mins[1], mins[2])
    size = (maxs[0] - mins[0], maxs[1] - mins[1], maxs[2] - mins[2])
    return origin, size


def _check_data_version(data_version: Any, *, source: str, where: str) -> int:
    if not isinstance(data_version, int) or isinstance(data_version, bool):
        raise BlueprintError(f"{source}: {where} 不是整数（实际 {data_version!r}）",
                             code="missing_field")
    lo, hi = SUPPORTED_DATA_VERSION_RANGE
    if not (lo <= data_version <= hi):
        raise BlueprintError(
            f"{source}: 数据版本 {data_version} 超出我认识的范围 "
            f"[{lo}, {hi}]（{lo} = 1.13 扁平化起点）—— 不猜格式，如实拒绝",
            code="unsupported_version",
            detail={"data_version": data_version, "supported": [lo, hi]})
    return data_version


def _int_triple(value: Any, *, source: str, where: str) -> tuple[int, int, int]:
    if isinstance(value, dict):
        try:
            return (int(value["x"]), int(value["y"]), int(value["z"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise BlueprintError(f"{source}: {where} 不是 {{x,y,z}}（{value!r}）",
                                 code="bad_field") from exc
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return (int(value[0]), int(value[1]), int(value[2]))
    raise BlueprintError(f"{source}: {where} 不是三元组（{value!r}）", code="bad_field")


# ══════════════════════════════════════════════════════════════════════
# 5. Litematica (.litematic)
# ══════════════════════════════════════════════════════════════════════


def _parse_litematic(root: dict, *, source: str, max_regions: int, max_blocks: int,
                     layout: str) -> Blueprint:
    version = root.get("Version")
    if not isinstance(version, int):
        raise BlueprintError(f"{source}: litematic 缺少整数 `Version` 字段", code="missing_field")
    if version not in SUPPORTED_LITEMATIC_FORMAT_VERSIONS:
        raise BlueprintError(
            f"{source}: litematic 格式版本 {version} 我不认识（我支持 "
            f"{list(SUPPORTED_LITEMATIC_FORMAT_VERSIONS)}）—— 不猜格式，如实拒绝",
            code="unsupported_version",
            detail={"format_version": version,
                    "supported": list(SUPPORTED_LITEMATIC_FORMAT_VERSIONS)})
    data_version = _check_data_version(root.get("MinecraftDataVersion"),
                                       source=source, where="MinecraftDataVersion")
    regions_tag = root.get("Regions")
    if not isinstance(regions_tag, dict):
        raise BlueprintError(f"{source}: litematic 缺少 `Regions` 复合标签", code="missing_field")
    if len(regions_tag) > max_regions:
        raise BlueprintError(
            f"{source}: region 数 {len(regions_tag)} 超过上限 {max_regions}",
            code="too_many_regions", detail={"regions": len(regions_tag), "limit": max_regions})

    regions: list[Region] = []
    be_all: list[dict] = []
    ent_all: list[dict] = []
    total = 0
    for name, raw in regions_tag.items():
        if not isinstance(raw, dict):
            raise BlueprintError(f"{source}: region {name!r} 不是复合标签", code="bad_field")
        origin = _int_triple(raw.get("Position"), source=source, where=f"region {name!r}.Position")
        size = _int_triple(raw.get("Size"), source=source, where=f"region {name!r}.Size")
        volume = abs(size[0]) * abs(size[1]) * abs(size[2])
        total += volume
        if total > max_blocks:
            raise BlueprintError(
                f"{source}: 总格子数 {total} 超过上限 {max_blocks}",
                code="too_many_blocks", detail={"blocks": total, "limit": max_blocks})
        palette_tag = raw.get("BlockStatePalette")
        if not isinstance(palette_tag, list):
            raise BlueprintError(f"{source}: region {name!r} 缺少 `BlockStatePalette`",
                                 code="missing_field")
        palette = _litematic_palette(palette_tag, source=source, region=name)
        states = raw.get("BlockStates")
        if not isinstance(states, LongArray):
            raise BlueprintError(
                f"{source}: region {name!r} 的 `BlockStates` 不是长整型数组"
                f"（TAG_Long_Array，实际 {type(states).__name__ if states is not None else '缺失'}）",
                code="missing_field")
        bits = max(2, (len(palette) - 1).bit_length()) if len(palette) > 1 else 2
        expected = (volume * bits + 63) // 64
        if len(states) < expected:
            raise BlueprintError(
                f"{source}: region {name!r} 的 BlockStates 长度 {len(states)} 少于所需的 "
                f"{expected}（{volume} 格 × {bits} 位）—— 文件截断或位宽不符",
                code="truncated",
                detail={"have": len(states), "need": expected})
        be = tuple(x for x in (raw.get("TileEntities") or []) if isinstance(x, dict))
        ents = tuple(x for x in (raw.get("Entities") or []) if isinstance(x, dict))
        be_all.extend(be)
        ent_all.extend(ents)
        regions.append(Region(name=str(name), origin=origin, size=size, palette=palette,
                              bits=bits, states=LongArray(states), block_entities=be,
                              entities=ents))

    meta = root.get("Metadata") if isinstance(root.get("Metadata"), dict) else {}
    origin, size = _bbox(regions)
    notes: list[str] = []
    if any(s < 0 for r in regions for s in r.size):
        notes.append("有 region 的 Size 含负值：本地坐标仍是 0..abs(size)-1，"
                     "世界坐标 = Position + 本地坐标（按 Litematica 的取向）")
    materials, excluded = _aggregate(regions)
    return Blueprint(
        format="litematic", format_version=version, data_version=data_version,
        size=size, origin=origin, regions=tuple(regions),
        materials=materials, excluded_materials=excluded,
        block_entities=tuple(be_all), entities=tuple(ent_all),
        name=meta.get("Name"), author=meta.get("Author"), description=meta.get("Description"),
        layout=layout, notes=tuple(notes), source=source)


def _litematic_palette(palette_tag: list, *, source: str, region: str) -> tuple[str, ...]:
    out: list[str] = []
    for i, entry in enumerate(palette_tag):
        if not isinstance(entry, dict):
            raise BlueprintError(f"{source}: region {region!r} 调色板第 {i} 项不是复合标签",
                                 code="bad_palette")
        name = entry.get("Name")
        if not isinstance(name, str) or not name:
            raise BlueprintError(f"{source}: region {region!r} 调色板第 {i} 项缺少 `Name`",
                                 code="bad_palette")
        props = entry.get("Properties")
        if isinstance(props, dict) and props:
            body = ",".join(f"{k}={v}" for k, v in sorted(props.items()))
            name = f"{name}[{body}]"
        out.append(name)
    if not out:
        raise BlueprintError(f"{source}: region {region!r} 调色板为空", code="bad_palette")
    return tuple(out)


# ══════════════════════════════════════════════════════════════════════
# 6. Sponge (.schem / .nbt)
# ══════════════════════════════════════════════════════════════════════


def _decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """LEB128（Sponge `BlockData`）。越界抛 `truncated`（不静默截断）。"""
    value = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise BlueprintError("Sponge BlockData：varint 在字节流末尾被截断",
                                 code="truncated")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7
        if shift > 35:
            raise BlueprintError("Sponge BlockData：varint 超过 5 字节（不是合法 32 位整数）",
                                 code="bad_field")


def _parse_sponge(root: dict, *, source: str, max_blocks: int, layout: str) -> Blueprint:
    version = root.get("Version")
    if not isinstance(version, int):
        raise BlueprintError(f"{source}: Sponge 蓝图缺少整数 `Version` 字段", code="missing_field")
    if version not in SUPPORTED_SPONGE_VERSIONS:
        extra = "（v1 是 MCEdit 血统，本层不支持）" if version == 1 else ""
        raise BlueprintError(
            f"{source}: Sponge 格式版本 {version} 我不认识（我支持 "
            f"{list(SUPPORTED_SPONGE_VERSIONS)}）{extra} —— 不猜格式，如实拒绝",
            code="unsupported_version",
            detail={"format_version": version, "supported": list(SUPPORTED_SPONGE_VERSIONS)})
    data_version = _check_data_version(root.get("DataVersion"),
                                       source=source, where="DataVersion")
    dims = []
    for axis in ("Width", "Height", "Length"):
        value = root.get(axis)
        if not isinstance(value, int) or isinstance(value, bool):
            raise BlueprintError(f"{source}: 缺少整数 `{axis}`", code="missing_field")
        if value <= 0:
            raise BlueprintError(f"{source}: `{axis}` = {value} 不是有效尺寸", code="bad_field")
        dims.append(value)
    width, height, length = dims
    volume = width * height * length
    if volume > max_blocks:
        raise BlueprintError(f"{source}: 格子数 {volume} 超过上限 {max_blocks}",
                             code="too_many_blocks",
                             detail={"blocks": volume, "limit": max_blocks})

    # v3：块数据在 `Blocks` 复合标签里；v2：在根上。
    notes: list[str] = []
    blocks_src = root
    if version == 3:
        inner = root.get("Blocks")
        if isinstance(inner, dict):
            blocks_src = inner
        else:
            notes.append("声明 v3 但根上没有 `Blocks` 复合标签，按 v2 布局读根级 Palette/Data")
            layout = layout + "+root-blocks"

    palette_tag = blocks_src.get("Palette")
    if not isinstance(palette_tag, dict) or not palette_tag:
        raise BlueprintError(f"{source}: 缺少 `Palette` 复合标签（块调色板）",
                             code="missing_field")
    palette = _sponge_palette(palette_tag, source=source)
    data = blocks_src.get("BlockData")
    if not isinstance(data, ByteArray):
        data = blocks_src.get("Data")
    if not isinstance(data, ByteArray):
        raise BlueprintError(f"{source}: 缺少 `BlockData`（v2）/ `Data`（v3）字节数组",
                             code="missing_field")

    # BlockData 是 varint 顺序流 —— **不整表展开**（几千万格的 Python 列表会吃 GB 级内存），
    # 存在 region 上按需顺序解码。
    notes.append("Sponge 的 BlockData 按 varint 顺序解码；逐块查询是按索引顺序走流，"
                 "不预先展开整张索引表")

    palette_max = blocks_src.get("PaletteMax")
    if isinstance(palette_max, int) and palette_max != len(palette):
        notes.append(f"`PaletteMax`={palette_max} 与调色板项数 {len(palette)} 不一致（已按 Palette 为准）")

    # Offset：v3 直接用根上的 Offset；v2 的 Offset 是世界原点相对，玩家相对偏移在 Metadata.WEOffset*
    meta = root.get("Metadata") if isinstance(root.get("Metadata"), dict) else {}
    if version == 2:
        raw_offset = root.get("Offset")
        origin = [0, 0, 0]
        for i, key in enumerate(("WEOffsetX", "WEOffsetY", "WEOffsetZ")):
            if isinstance(meta.get(key), int):
                origin[i] = int(meta[key])
            elif isinstance(raw_offset, (IntArray, list)) and len(raw_offset) >= 3:
                origin[i] = int(raw_offset[i])
        if any(isinstance(meta.get(k), int) for k in ("WEOffsetX", "WEOffsetY", "WEOffsetZ")):
            notes.append("v2：Offset 是世界原点相对，玩家相对偏移取自 Metadata.WEOffset*")
    else:
        raw_offset = root.get("Offset")
        origin = [0, 0, 0]
        if isinstance(raw_offset, (IntArray, list)) and len(raw_offset) >= 3:
            origin = [int(raw_offset[0]), int(raw_offset[1]), int(raw_offset[2])]

    region = _SpongeRegion(
        name="schematic", origin=(origin[0], origin[1], origin[2]), size=(width, height, length),
        palette=palette, data=data)
    materials, excluded = _aggregate((region,))   # 这一趟同时验证 varint 流不会中途断掉
    if region.consumed_bytes is not None and region.consumed_bytes != len(data):
        notes.append(f"BlockData 尾部多出 {len(data) - region.consumed_bytes} 字节（已忽略）")
    be_tag = blocks_src.get("BlockEntities")
    ent_tag = root.get("Entities")
    be = tuple(x for x in be_tag if isinstance(x, dict)) if isinstance(be_tag, list) else ()
    ents = tuple(x for x in ent_tag if isinstance(x, dict)) if isinstance(ent_tag, list) else ()

    # 单一 region ⇒ 包围盒就是它自己
    return Blueprint(
        format="sponge", format_version=version, data_version=data_version,
        size=(width, height, length), origin=(origin[0], origin[1], origin[2]),
        regions=(region,),  # type: ignore[arg-type]
        materials=materials, excluded_materials=excluded,
        block_entities=be, entities=ents,
        name=meta.get("Name"), author=meta.get("Author"), description=meta.get("Description"),
        layout=layout, notes=tuple(notes), source=source)


class _SpongeRegion(Region):
    """Sponge 的块数据是 varint 索引数组（不是位打包），所以单独一个小类。

    索引顺序与 Litematica 一致：`x + z*Width + y*Width*Length`（x 最快）。
    原始字节只保留一份；`iter_local()` 顺序走流解码（内存有界），
    随机访问 `palette_index()` 是 O(n)（仅供偶发查询，摘要/逐块路径不用它）。
    """

    def __init__(self, *, name: str, origin: tuple[int, int, int], size: tuple[int, int, int],
                 palette: tuple[str, ...], data: ByteArray) -> None:
        super().__init__(name=name, origin=origin, size=size, palette=palette, bits=0,
                         states=LongArray())
        self.data = data
        self.consumed_bytes: Optional[int] = None  # 走完一整趟后记录用掉多少字节

    def palette_index(self, index: int) -> int:
        if not (0 <= index < self.volume):
            raise BlueprintError(
                f"Sponge 蓝图：块索引 {index} 越界（共 {self.volume} 格）", code="truncated")
        pos = 0
        value = 0
        for _ in range(index + 1):
            value, pos = _decode_varint(self.data, pos)
        if index + 1 == self.volume:
            self.consumed_bytes = pos
        return value

    def _block(self, pid: int) -> str:
        if pid < 0 or pid >= len(self.palette):
            raise BlueprintError(
                f"Sponge 蓝图：BlockData 里出现调色板索引 {pid}，但 Palette 只有 "
                f"{len(self.palette)} 项", code="bad_palette")
        block = self.palette[pid]
        if block is _HOLE:
            raise BlueprintError(
                f"Sponge 蓝图：调色板索引 {pid} 是空洞（索引不连续），不猜它是什么方块",
                code="bad_palette")
        return block

    def iter_local(self, *, y_range: Optional[tuple[int, int]] = None,
                   box: Optional[tuple[int, int, int, int, int, int]] = None,
                   include_air: bool = True, limit: Optional[int] = None
                   ) -> Iterator[tuple[int, int, int, str]]:
        ax, ay, az = self.abs_size
        x0, x1, z0, z1 = 0, ax, 0, az
        y0, y1 = 0, ay
        ox, oy, oz = self.origin
        if y_range is not None:
            y0 = max(0, int(y_range[0]) - oy); y1 = min(ay, int(y_range[1]) - oy)
        if box is not None:
            bx0, by0, bz0, bx1, by1, bz1 = (int(v) for v in box)
            x0 = max(x0, bx0 - ox); x1 = min(x1, bx1 - ox)
            y0 = max(y0, by0 - oy); y1 = min(y1, by1 - oy)
            z0 = max(z0, bz0 - oz); z1 = min(z1, bz1 - oz)
        pos = 0
        emitted = 0
        full = (x0, x1, z0, z1, y0, y1) == (0, ax, 0, az, 0, ay)
        for ly in range(ay):          # varint 是顺序流 ⇒ 逐格解码、只落地命中的那批
            for lz in range(az):
                for lx in range(ax):
                    pid, pos = _decode_varint(self.data, pos)
                    if not (y0 <= ly < y1 and z0 <= lz < z1 and x0 <= lx < x1):
                        continue
                    block = self._block(pid)
                    if not include_air and block.split("[", 1)[0] in AIR_BLOCKS:
                        continue
                    yield (lx, ly, lz, block)
                    emitted += 1
                    if limit is not None and emitted >= limit:
                        return
        if full:
            self.consumed_bytes = pos


def _sponge_palette(palette_tag: dict, *, source: str) -> tuple[str, ...]:
    max_id = -1
    for key, value in palette_tag.items():
        if not isinstance(value, int) or isinstance(value, bool):
            raise BlueprintError(f"{source}: Palette 项 {key!r} 的索引不是整数",
                                 code="bad_palette")
        if value < 0:
            raise BlueprintError(f"{source}: Palette 项 {key!r} 的索引为负（{value}）",
                                 code="bad_palette")
        max_id = max(max_id, value)
    if max_id < 0:
        raise BlueprintError(f"{source}: Palette 为空", code="bad_palette")
    out: list[Optional[str]] = [None] * (max_id + 1)
    for key, value in palette_tag.items():
        if out[value] is not None and out[value] != key:
            raise BlueprintError(f"{source}: Palette 索引 {value} 被 {out[value]!r} 与 "
                                 f"{key!r} 重复占用", code="bad_palette")
        out[value] = str(key)
    # 空洞（索引不连续）不猜内容：用到时如实报错
    return tuple(b if b is not None else _HOLE for b in out)


_HOLE = "<palette-hole>"


# ══════════════════════════════════════════════════════════════════════
# 7. 入口
# ══════════════════════════════════════════════════════════════════════


def _dispatch(root: dict, *, source: str, max_regions: int, max_blocks: int) -> Blueprint:
    layout = "root"
    # 少数写入器把整个 schematic 包在 `Schematic` 复合标签里（v3 家族）
    inner = root.get("Schematic")
    if isinstance(inner, dict) and "Version" in inner:
        root = inner
        layout = "schematic-wrapper"

    regions_tag = root.get("Regions")
    if isinstance(regions_tag, dict):
        return _parse_litematic(root, source=source, max_regions=max_regions,
                                max_blocks=max_blocks, layout=layout)

    blocks_tag = root.get("Blocks")
    if isinstance(blocks_tag, ByteArray):
        raise BlueprintError(
            f"{source}: 这是 legacy MCEdit `.schematic`（`Blocks` 是 byte[]、`Materials` 是文本表）"
            "—— 本层明确不支持这种格式（请转成 .schem / .litematic 再来）",
            code="legacy_schematic")

    if "Version" in root:
        return _parse_sponge(root, source=source, max_blocks=max_blocks, layout=layout)

    if all(k in root for k in ("Width", "Height", "Length")):
        raise BlueprintError(
            f"{source}: 有 Width/Height/Length 但没有 `Version` 字段 —— 无法确定是 "
            "Sponge v2/v3 还是 legacy .schematic，不猜格式，如实拒绝",
            code="missing_field")

    raise BlueprintError(
        f"{source}: 认不出这是什么蓝图（根标签里既没有 `Regions`（litematic）"
        "也没有 `Version`（Sponge））—— 不猜格式，如实拒绝",
        code="unsupported_format", detail={"root_keys": sorted(root.keys())[:20]})


def parse_blueprint_bytes(raw: bytes, *, source: str = "<bytes>",
                          max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
                          max_regions: int = DEFAULT_MAX_REGIONS,
                          max_blocks: int = DEFAULT_MAX_BLOCKS) -> Blueprint:
    """从内存字节解析（测试用；也方便调用方自己管 IO）。"""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise BlueprintError("输入不是 bytes", code="bad_field")
    root = _read_root_nbt(bytes(raw), source=source, max_decompressed=max_decompressed_bytes)
    return _dispatch(root, source=source, max_regions=max_regions, max_blocks=max_blocks)


def load_blueprint(path: "str | os.PathLike[str]", *,
                   max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
                   max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
                   max_regions: int = DEFAULT_MAX_REGIONS,
                   max_blocks: int = DEFAULT_MAX_BLOCKS) -> Blueprint:
    """读一个蓝图文件并解析。

    只读；不认识 / 版本不支持 / 超上限 **一律如实抛 `BlueprintError`**（带 `code`），
    绝不猜格式、绝不假装能盖。
    """
    p = os.fspath(path)
    if not os.path.isfile(p):
        raise BlueprintError(f"蓝图文件不存在：{p}", code="not_found")
    size_bytes = os.path.getsize(p)
    if size_bytes > max_file_bytes:
        raise BlueprintError(
            f"{os.path.basename(p)} 大小 {size_bytes} 字节超过上限 {max_file_bytes} 字节",
            code="too_large", detail={"bytes": size_bytes, "limit": max_file_bytes})
    with open(p, "rb") as fh:
        raw = fh.read(max_file_bytes + 1)
    return parse_blueprint_bytes(raw, source=os.path.basename(p),
                                 max_decompressed_bytes=max_decompressed_bytes,
                                 max_regions=max_regions, max_blocks=max_blocks)


# ══════════════════════════════════════════════════════════════════════
# 8. 给模型的渲染
# ══════════════════════════════════════════════════════════════════════

TOOL_HELP = (
    "读一份**已经放在后端上的**蓝图文件（Litematica .litematic / Sponge .schem / .nbt），"
    "给出尺寸、逐 region 原点与尺寸、材料清单（按数量）、以及**盖不了的项**。"
    "只读、不建造、不改世界。适合「照着这个蓝图盖」「要备多少料」这类问题；"
    "要逐块盖请用 blueprint_blocks 分片取块（别一次要几十万块）。"
    "不给文件名时如实报错，绝不猜路径。"
)


def _fmt(n: int) -> str:
    return f"{int(n):,}"


def _fmt_block(block: str) -> str:
    return block


def render_summary(bp: Blueprint, *, lang: str = "zh", top_materials: int = 15,
                   top_problems: int = 5) -> str:
    """人读摘要（给 LLM 用）。**只给计数与前 N 项**，绝不塞逐块数据。

    lang="zh"（默认）或 "en"。
    """
    if lang not in ("zh", "en"):
        raise ValueError("lang 只能是 'zh' 或 'en'")
    sx, sy, sz = bp.size
    fam = {"litematic": "Litematica .litematic", "sponge": "Sponge .schem"}.get(bp.format, bp.format)
    placeable = bp.placeable_materials
    excluded_blocks = {m.block for m in bp.excluded_materials}   # 空气类已在尺寸行说明
    problems = tuple(m for m in bp.not_placeable if m.block not in excluded_blocks)
    unverified = bp.unverified
    prop = bp.needs_property

    if lang == "en":
        return _render_en(bp, top_materials, top_problems, fam, placeable, problems,
                          unverified, prop)

    lines: list[str] = []
    title = bp.name or "(未命名)"
    lines.append(f"【蓝图】{title} · {fam}")
    lines.append(f"格式版本 {bp.format_version} · 数据版本 {bp.data_version}"
                 + (f" · 布局 {bp.layout}" if bp.layout else ""))
    lines.append(f"尺寸 {sx}×{sy}×{sz}（包围盒原点 {bp.origin[0]},{bp.origin[1]},{bp.origin[2]}）"
                 f" · 总 {_fmt(bp.total_volume)} 格（其中空气 {_fmt(bp.air_blocks)} 格）")
    lines.append(f"region {len(bp.regions)} 个：" + ", ".join(
        f"{r.name}(尺寸 {abs(r.size[0])}×{abs(r.size[1])}×{abs(r.size[2])}"
        f" @ {r.origin[0]},{r.origin[1]},{r.origin[2]})" for r in bp.regions[:6])
        + (" …" if len(bp.regions) > 6 else ""))
    lines.append("")
    if not bp.materials:
        lines.append("整份蓝图只有空气 —— 没有要准备的材料。")
    else:
        lines.append(f"要准备的材料 {len(placeable)} 种（合计 {_fmt(sum(m.count for m in placeable))} 格）：")
        for m in placeable[:top_materials]:
            lines.append(f"  {_fmt_block(m.block)} ×{_fmt(m.count)}")
        rest = len(placeable) - top_materials
        if rest > 0:
            lines.append(f"  …还有 {rest} 种（用 blueprint_materials 查全部）")
    lines.append("")

    if prop:
        shown = prop[:top_problems]
        lines.append(f"朝向可能不对 {len(prop)} 种（合计 "
                     f"{_fmt(sum(m.count for m in prop))} 格）：这些方块带状态属性"
                     "（楼梯朝向／原木轴向／观察者朝向…）；当前放置只按**方块类型**核对，"
                     "盖出来朝向可能不对。例如：" + "；".join(f"{m.block} ×{_fmt(m.count)}" for m in shown))
    if problems:
        shown = problems[:top_problems]
        lines.append(f"拿不到 / 需要确认 {len(problems)} 种（合计 "
                     f"{_fmt(sum(m.count for m in problems))} 格）：")
        for m in shown:
            note = m.notes[0] if m.notes else ""
            lines.append(f"  {_fmt_block(m.block)} ×{_fmt(m.count)} —— {note}")
        if len(problems) > top_problems:
            lines.append(f"  …还有 {len(problems) - top_problems} 种")
    if unverified:
        lines.append(f"第三方模组方块 {len(unverified)} 种（合计 "
                     f"{_fmt(sum(m.count for m in unverified))} 格）：有没有对应物品我无法确认，"
                     "需人工确认。例如：" + "、".join(m.block for m in unverified[:top_problems]))
    if bp.block_entities or bp.entities:
        lines.append(f"方块实体 {len(bp.block_entities)} 个、实体 {len(bp.entities)} 个 —— "
                     "这些我盖不了（NBT 内容只解析出来供参考，不逐项复现）")
    for note in bp.notes:
        lines.append(f"备注：{note}")
    lines.append("（逐块位置不在这里 —— 要盖请用分片查询按 region / y 层 / 坐标框一块一块取）")
    return "\n".join(lines)


def _render_en(bp: Blueprint, top_materials: int, top_problems: int, fam: str,
               placeable: Sequence[Material], problems: Sequence[Material],
               unverified: Sequence[Material], prop: Sequence[Material]) -> str:
    sx, sy, sz = bp.size
    lines: list[str] = []
    lines.append(f"[Blueprint] {bp.name or '(unnamed)'} - {fam}")
    lines.append(f"format v{bp.format_version}, data version {bp.data_version}"
                 + (f", layout {bp.layout}" if bp.layout else ""))
    lines.append(f"size {sx}x{sy}x{sz} (bbox origin {bp.origin[0]},{bp.origin[1]},{bp.origin[2]})"
                 f" - {_fmt(bp.total_volume)} cells (air {_fmt(bp.air_blocks)})")
    lines.append(f"{len(bp.regions)} region(s): " + ", ".join(
        f"{r.name}({abs(r.size[0])}x{abs(r.size[1])}x{abs(r.size[2])}"
        f" @ {r.origin[0]},{r.origin[1]},{r.origin[2]})" for r in bp.regions[:6])
        + (" ..." if len(bp.regions) > 6 else ""))
    lines.append("")
    if not bp.materials:
        lines.append("Air only - nothing to gather.")
    else:
        lines.append(f"Materials to gather: {len(placeable)} kind(s), "
                     f"{_fmt(sum(m.count for m in placeable))} block(s):")
        for m in placeable[:top_materials]:
            lines.append(f"  {m.block} x{_fmt(m.count)}")
        rest = len(placeable) - top_materials
        if rest > 0:
            lines.append(f"  ... {rest} more (use blueprint_materials for the full list)")
    lines.append("")
    if prop:
        lines.append(f"{len(prop)} kind(s) ({_fmt(sum(m.count for m in prop))} blocks) carry "
                     "block-state properties (stair facing, log axis, ...): the current place "
                     "path only checks the block TYPE, so orientation may come out wrong. e.g. "
                     + "; ".join(f"{m.block} x{_fmt(m.count)}" for m in prop[:top_problems]))
    if problems:
        lines.append(f"{len(problems)} kind(s) ({_fmt(sum(m.count for m in problems))} blocks) "
                     "cannot be materialised:")
        for m in problems[:top_problems]:
            lines.append(f"  {m.block} x{_fmt(m.count)} - {m.notes[0] if m.notes else ''}")
        if len(problems) > top_problems:
            lines.append(f"  ... {len(problems) - top_problems} more")
    if unverified:
        lines.append(f"{len(unverified)} kind(s) ({_fmt(sum(m.count for m in unverified))} "
                     "blocks) from modded namespaces - cannot confirm they have an item; needs "
                     "human confirmation: " + ", ".join(m.block for m in unverified[:top_problems]))
    if bp.block_entities or bp.entities:
        lines.append(f"{len(bp.block_entities)} block entities, {len(bp.entities)} entities - "
                     "not placeable by me (parsed for reference only)")
    for note in bp.notes:
        lines.append(f"note: {note}")
    lines.append("(Per-block positions are not included - query shards by region / y-layer / box)")
    return "\n".join(lines)
