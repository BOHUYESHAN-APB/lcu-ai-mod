"""`agent.blueprint` 的纯逻辑自检 —— 全部用**自造的最小 NBT 字节**当夹具。

不信任何真实文件、不碰 `data/`、不联网；只在临时目录里写小文件（用完即删，
仓库里不留二进制夹具）。覆盖：

  - litematic 2×2×2 单方块：尺寸 / 材料清单 / 逐块相对坐标**逐条断言**
  - 空气块：不进材料清单，但占位/尺寸正确（并出现在 `not_placeable`）
  - 带属性方块（楼梯朝向）⇒ 出现在 `needs_property`
  - 不认识的格式版本 ⇒ **如实拒绝**且错误信息里带版本号
  - 位打包**跨 long 边界**的 litematic ⇒ 解出的索引序列正确
  - **负数 origin 的多 region** litematic ⇒ 世界坐标换算正确
  - 超大文件 / 超多 region / 超多格子 ⇒ 触发上限并如实报错（不崩、不 OOM）
  - legacy `.schematic` ⇒ 明确拒绝
  - Sponge v2 / v3：varint、索引顺序、Offset 语义
  - 摘要渲染（中/英）不塞逐块数据、超长时提示分页
"""

from __future__ import annotations

import gzip
import tempfile
import unittest
import zlib
from pathlib import Path

from agent.blueprint import (BlueprintError, load_blueprint, parse_blueprint_bytes,
                             render_summary)


# ══════════════════════════════════════════════════════════════════════
# 夹具：最小 NBT 写入器（大端；直接把已编码的 payload 拼起来）
# ══════════════════════════════════════════════════════════════════════

def _s(text: str) -> bytes:
    raw = text.encode("utf-8")
    return len(raw).to_bytes(2, "big", signed=False) + raw


def _i32(value) -> bytes:
    return int(value).to_bytes(4, "big", signed=True)


def _i16(value) -> bytes:
    return int(value).to_bytes(2, "big", signed=True)


def _i64(value) -> bytes:
    return int(value).to_bytes(8, "big", signed=True)


def _i64_signed(value: int) -> int:
    """把 64 位无符号整数变回 NBT LONG 的带符号值（Java long 语义）。"""
    value &= (1 << 64) - 1
    return value - (1 << 64) if value >= (1 << 63) else value


def B(value):        # TAG_Byte
    return (1, bytes([int(value) & 0xFF]))


def SH(value):       # TAG_Short
    return (2, _i16(value))


def I(value):        # TAG_Int
    return (3, _i32(value))


def L(value):        # TAG_Long
    return (4, _i64(value))


def STR(text):       # TAG_String
    return (8, _s(text))


def LIST(elem_tag: int, payloads: list[bytes]):
    return (9, bytes([elem_tag]) + _i32(len(payloads)) + b"".join(payloads))


def COMPOUND(fields: dict):
    body = b""
    for key, (tag, payload) in fields.items():
        body += bytes([tag]) + _s(key) + payload
    return (10, body + b"\x00")


def INT_ARRAY(values):
    return (11, _i32(len(values)) + b"".join(_i32(v) for v in values))


def LONG_ARRAY(values):
    return (12, _i32(len(values)) + b"".join(_i64(_i64_signed(v)) for v in values))


def BYTE_ARRAY(raw: bytes):
    return (7, _i32(len(raw)) + bytes(raw))


def _root(fields: dict, *, compress: str = "gzip") -> bytes:
    raw = b"\x0a" + _s("") + COMPOUND(fields)[1]
    if compress == "gzip":
        return gzip.compress(raw)
    if compress == "zlib":
        return zlib.compress(raw)
    return raw


# ── litematic 位打包（与 Litematica `LitematicaBitArray` 一致：条目**可跨 long**）──

def _bits_for(palette_size: int) -> int:
    return max(2, (palette_size - 1).bit_length())


def _pack_tight(values: list[int], bits: int) -> list[int]:
    longs = [0] * (((len(values) * bits) + 63) // 64)
    mask = (1 << bits) - 1
    for index, value in enumerate(values):
        value &= mask
        start_offset = index * bits
        start = start_offset >> 6
        end = ((index + 1) * bits - 1) >> 6
        bit = start_offset & 0x3F
        longs[start] |= value << bit
        if start != end:
            longs[end] |= value >> (64 - bit)
    return [v & ((1 << 64) - 1) for v in longs]


def _region(name: str, pos, size, palette: list[tuple[str, dict] | str],
            values: list[int], *, tile_entities=None):
    """构造一个 region 复合标签（返回可直接放进 `Regions` 的 (tag, payload)）。"""
    entries = []
    for entry in palette:
        if isinstance(entry, str):
            fields = {"Name": STR(entry)}
        else:
            block, props = entry
            block_fields = {"Name": STR(block)}
            if props:
                block_fields["Properties"] = COMPOUND({k: STR(v) for k, v in props.items()})
            fields = block_fields
        entries.append(COMPOUND(fields)[1])
    bits = _bits_for(len(palette))
    longs = _pack_tight(values, bits)
    fields = {
        "Position": COMPOUND({"x": I(pos[0]), "y": I(pos[1]), "z": I(pos[2])}),
        "Size": COMPOUND({"x": I(size[0]), "y": I(size[1]), "z": I(size[2])}),
        "BlockStatePalette": LIST(10, entries),
        "BlockStates": LONG_ARRAY(longs),
    }
    if tile_entities is not None:
        fields["TileEntities"] = LIST(10, [COMPOUND(t)[1] for t in tile_entities])
    return COMPOUND(fields)


def litematic(regions: dict, *, version: int = 6, data_version: int = 3953,
              metadata: dict | None = None, compress: str = "gzip") -> bytes:
    fields: dict = {
        "Version": I(version),
        "MinecraftDataVersion": I(data_version),
        "Regions": COMPOUND(dict(regions)),
    }
    if metadata is not None:
        fields["Metadata"] = COMPOUND(metadata)
    return _root(fields, compress=compress)


def _index(size, lx: int, ly: int, lz: int) -> int:
    """x + z*Width + y*Width*Length（x 最快）——litematic 与 Sponge 同序。"""
    ax, _ay, az = abs(size[0]), abs(size[1]), abs(size[2])
    return (ly * az + lz) * ax + lx


# ── Sponge ──

def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def sponge(width: int, height: int, length: int, palette: dict[str, int],
           ids: list[int], *, version: int = 2, data_version: int = 4189,
           offset=None, we_offset=None, palette_max=None,
           block_entities=None, nested: bool = False) -> bytes:
    assert len(ids) == width * height * length
    data = b"".join(_varint(v) for v in ids)
    pal = COMPOUND({name: I(idx) for name, idx in palette.items()})
    fields: dict = {
        "Version": I(version),
        "DataVersion": I(data_version),
        "Width": SH(width),
        "Height": SH(height),
        "Length": SH(length),
    }
    if version == 3:
        blocks_fields = {"Palette": pal, "Data": BYTE_ARRAY(data)}
        if block_entities is not None:
            blocks_fields["BlockEntities"] = LIST(10, [COMPOUND(b)[1] for b in block_entities])
        fields["Blocks"] = COMPOUND(blocks_fields)
        if offset is not None:
            fields["Offset"] = INT_ARRAY(offset)
    else:
        fields["Palette"] = pal
        fields["BlockData"] = BYTE_ARRAY(data)
        fields["PaletteMax"] = I(palette_max if palette_max is not None else len(palette))
        if offset is not None:
            fields["Offset"] = INT_ARRAY(offset)
        if block_entities is not None:
            fields["BlockEntities"] = LIST(10, [COMPOUND(b)[1] for b in block_entities])
    if we_offset is not None:
        fields["Metadata"] = COMPOUND({"Name": STR("fixture"),
                                       "WEOffsetX": I(we_offset[0]),
                                       "WEOffsetY": I(we_offset[1]),
                                       "WEOffsetZ": I(we_offset[2])})
    if nested:
        return _root({"Schematic": COMPOUND(fields)})
    return _root(fields)


class _TempMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name: str, raw: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(raw)
        return path


# ══════════════════════════════════════════════════════════════════════
# 1. litematic 基础：尺寸 / 材料 / 逐块坐标
# ══════════════════════════════════════════════════════════════════════

class LitematicBasicTests(_TempMixin):
    def test_2x2x2_single_block_everything_asserted(self):
        size = (2, 2, 2)
        raw = litematic({"main": _region("main", (0, 64, 0), size,
                                        [("minecraft:stone", {})], [0] * 8)})
        bp = load_blueprint(self.write("a.litematic", raw))

        self.assertEqual(bp.format, "litematic")
        self.assertEqual(bp.format_version, 6)
        self.assertEqual(bp.data_version, 3953)
        self.assertEqual(bp.size, (2, 2, 2))
        self.assertEqual(bp.origin, (0, 64, 0))
        self.assertEqual(bp.total_volume, 8)
        self.assertEqual(bp.air_blocks, 0)
        self.assertEqual([m.block for m in bp.materials], ["minecraft:stone"])
        self.assertEqual([m.count for m in bp.materials], [8])
        self.assertEqual(bp.placeable_materials[0].placeable, True)
        self.assertEqual(bp.not_placeable, ())

        got = {(x, y, z): b for x, y, z, b in bp.iter_blocks()}
        expect = {(x, 64 + y, z): "minecraft:stone"
                  for y in range(2) for z in range(2) for x in range(2)}
        self.assertEqual(got, expect)

    def test_relative_coords_follow_blockstates_order(self):
        """把逐块坐标按**索引顺序**钉死：x 最快、然后 z、然后 y。"""
        size = (2, 1, 2)
        ids = [0, 1, 2, 0]                     # (0,0,0)(1,0,0)(0,0,1)(1,0,1)
        raw = litematic({"main": _region("main", (-3, 5, -2), size,
                                        [("minecraft:stone", {}), ("minecraft:dirt", {}),
                                         ("minecraft:oak_planks", {})], ids)})
        bp = load_blueprint(self.write("b.litematic", raw))
        self.assertEqual(list(bp.iter_blocks()), [
            (-3, 5, -2, "minecraft:stone"),
            (-2, 5, -2, "minecraft:dirt"),
            (-3, 5, -1, "minecraft:oak_planks"),
            (-2, 5, -1, "minecraft:stone"),
        ])

    def test_air_excluded_from_materials_but_kept_in_place(self):
        size = (2, 2, 2)
        # 交替 stone / air
        ids = []
        for i in range(8):
            ids.append(0 if i % 2 == 0 else 1)
        raw = litematic({"main": _region("main", (0, 0, 0), size,
                                        [("minecraft:stone", {}), ("minecraft:air", {})], ids)})
        bp = load_blueprint(self.write("c.litematic", raw))
        self.assertEqual([(m.block, m.count) for m in bp.materials], [("minecraft:stone", 4)])
        self.assertEqual(bp.total_volume, 8)          # 占位/尺寸照算
        self.assertEqual(bp.air_blocks, 4)
        self.assertEqual(bp.size, (2, 2, 2))
        self.assertNotIn("minecraft:air", [m.block for m in bp.materials])
        air = [m for m in bp.not_placeable if m.block == "minecraft:air"]
        self.assertEqual(len(air), 1)
        self.assertEqual(air[0].count, 4)
        self.assertIn("not_an_item", air[0].tags)
        # include_air=False 时逐块查询把空气过滤掉
        self.assertEqual(len(list(bp.iter_blocks(include_air=False))), 4)

    def test_property_block_lands_in_needs_property(self):
        stairs = [("minecraft:oak_stairs",
                   {"facing": "east", "half": "bottom", "shape": "straight",
                    "waterlogged": "false"})]
        raw = litematic({"main": _region("main", (0, 0, 0), (2, 1, 1), stairs, [0, 0])})
        bp = load_blueprint(self.write("d.litematic", raw))
        block = "minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]"
        self.assertEqual([m.block for m in bp.placeable_materials], [])   # 归到"属性"那栏
        self.assertEqual([m.block for m in bp.needs_property], [block])
        self.assertEqual(bp.needs_property[0].count, 2)
        self.assertIn("isAlreadyBlock", bp.needs_property[0].notes[0])
        summary = render_summary(bp)
        self.assertIn("朝向可能不对", summary)

    def test_classification_buckets(self):
        palette = [("minecraft:bedrock", {}), ("minecraft:water", {}),
                   ("create:cogwheel", {}), ("minecraft:potted_oak_sapling", {}),
                   ("minecraft:stone", {})]
        size = (5, 1, 1)
        raw = litematic({"main": _region("main", (0, 0, 0), size, palette, [0, 1, 2, 3, 4])})
        bp = load_blueprint(self.write("e.litematic", raw))
        kinds = {m.block: m.tags for m in bp.materials}
        self.assertIn("creative_only", kinds["minecraft:bedrock"])
        self.assertIn("fluid", kinds["minecraft:water"])
        self.assertIn("unverified", kinds["create:cogwheel"])
        self.assertIn("no_direct_item", kinds["minecraft:potted_oak_sapling"])
        self.assertEqual(kinds["minecraft:stone"], frozenset())
        self.assertEqual([m.block for m in bp.placeable_materials], ["minecraft:stone"])
        self.assertEqual([m.block for m in bp.unverified], ["create:cogwheel"])
        problems = {m.block for m in bp.not_placeable}
        self.assertTrue({"minecraft:bedrock", "minecraft:water",
                         "minecraft:potted_oak_sapling"} <= problems)
        self.assertNotIn("minecraft:stone", problems)

    def test_metadata_and_block_entities_reported(self):
        be = [{"Id": STR("minecraft:chest"), "Pos": INT_ARRAY([1, 0, 0])}]
        raw = litematic(
            {"main": _region("main", (0, 0, 0), (2, 1, 1),
                             [("minecraft:chest", {})], [0, 0], tile_entities=be)},
            metadata={"Name": STR("小屋"), "Author": STR("某人"), "Description": STR("测试")})
        bp = load_blueprint(self.write("f.litematic", raw))
        self.assertEqual(bp.name, "小屋")
        self.assertEqual(bp.author, "某人")
        self.assertEqual(bp.description, "测试")
        self.assertEqual(len(bp.block_entities), 1)
        self.assertIn("方块实体 1 个", render_summary(bp))


# ══════════════════════════════════════════════════════════════════════
# 2. 位打包（跨 long 边界）
# ══════════════════════════════════════════════════════════════════════

class BitPackingTests(_TempMixin):
    def test_tight_packing_crossing_long_boundary(self):
        # 32 项调色板 ⇒ 5 位/项；13 格 ⇒ 65 位 ⇒ 2 个 long，最后一项跨边界
        palette = [(f"minecraft:block_{i:02d}", {}) for i in range(32)]
        values = list(range(13))
        size = (13, 1, 1)
        raw = litematic({"r": _region("r", (0, 0, 0), size, palette, values)})
        bp = load_blueprint(self.write("pack.litematic", raw))

        region = bp.regions[0]
        self.assertEqual(region.bits, 5)
        self.assertEqual(len(region.states), 2)
        # 首个跨边界的索引：60 + 5 > 64 ⇒ index 12（以及可能更多）
        crossing = [i for i in range(13) if (i * 5) >> 6 != ((i + 1) * 5 - 1) >> 6]
        self.assertEqual(crossing, [12])
        self.assertEqual([region.palette_index(i) for i in range(13)], values)
        self.assertEqual([b for *_c, b in region.iter_blocks()],
                         [f"minecraft:block_{v:02d}" for v in values])
        # 材料清单：13 种各 1 个
        self.assertEqual(len(bp.materials), 13)
        self.assertEqual(sorted(m.count for m in bp.materials), [1] * 13)

    def test_truncated_blockstates_rejected(self):
        palette = [(f"minecraft:block_{i:02d}", {}) for i in range(32)]
        size = (13, 1, 1)
        raw = litematic({"r": _region("r", (0, 0, 0), size, palette, list(range(13)))})
        # 手动把 BlockStates 砍短（重新构造一份）
        longs = _pack_tight(list(range(13)), 5)[:1]
        region_fields = {
            "Position": COMPOUND({"x": I(0), "y": I(0), "z": I(0)}),
            "Size": COMPOUND({"x": I(13), "y": I(1), "z": I(1)}),
            "BlockStatePalette": LIST(10, [
                COMPOUND({"Name": STR(name)})[1] for name, _ in palette]),
            "BlockStates": LONG_ARRAY(longs),
        }
        raw = litematic({"r": COMPOUND(region_fields)})
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(self.write("short.litematic", raw))
        self.assertEqual(ctx.exception.code, "truncated")
        self.assertIn("BlockStates", str(ctx.exception))


# ══════════════════════════════════════════════════════════════════════
# 3. 负数 origin / 多 region
# ══════════════════════════════════════════════════════════════════════

class MultiRegionTests(_TempMixin):
    def test_negative_origin_multi_region_world_coords(self):
        regions = {
            "a": _region("a", (-4, 60, 7), (2, 1, 2),
                         [("minecraft:stone", {}), ("minecraft:dirt", {})], [0, 1, 1, 0]),
            "b": _region("b", (10, -3, -5), (1, 2, 1),
                         [("minecraft:glass", {})], [0, 0]),
        }
        raw = litematic(regions)
        bp = load_blueprint(self.write("multi.litematic", raw))
        self.assertEqual(bp.region_names, ("a", "b"))
        # 包围盒：min = (-4, -3, -5)；max = (10+1, 60+1, 7+2) = (11, 61, 9)
        self.assertEqual(bp.origin, (-4, -3, -5))
        self.assertEqual(bp.size, (11 - (-4), 61 - (-3), 9 - (-5)))
        self.assertEqual(bp.total_volume, 4 + 2)
        self.assertEqual([(m.block, m.count) for m in bp.materials],
                         [("minecraft:dirt", 2), ("minecraft:glass", 2), ("minecraft:stone", 2)])
        self.assertEqual(set(bp.iter_blocks(region="a")), {
            (-4, 60, 7, "minecraft:stone"), (-3, 60, 7, "minecraft:dirt"),
            (-4, 60, 8, "minecraft:dirt"), (-3, 60, 8, "minecraft:stone"),
        })
        self.assertEqual(set(bp.iter_blocks(region="b")), {
            (10, -3, -5, "minecraft:glass"), (10, -2, -5, "minecraft:glass"),
        })
        # 坐标框分片（世界坐标，min 含 / max 不含）：只落下 region a 的 (0,0,0) 一格
        self.assertEqual(list(bp.iter_blocks(box=(-4, 60, 7, -3, 61, 8))),
                         [(-4, 60, 7, "minecraft:stone")])
        # y 层分片
        self.assertEqual([(x, y, z) for x, y, z, _b in bp.iter_blocks(y_range=(60, 61))],
                         [(-4, 60, 7), (-3, 60, 7), (-4, 60, 8), (-3, 60, 8)])

    def test_negative_size_uses_abs_for_indexing(self):
        # Size 含负值：本地坐标仍是 0..abs-1，世界坐标 = Position + 本地
        raw = litematic({"n": _region("n", (5, 2, -7), (2, -3, 1),
                                      [("minecraft:gold_block", {})], [0] * 6)})
        bp = load_blueprint(self.write("neg.litematic", raw))
        region = bp.regions[0]
        self.assertEqual(region.abs_size, (2, 3, 1))
        self.assertEqual(region.volume, 6)
        self.assertEqual(bp.size, (2, 3, 1))
        self.assertEqual([(m.block, m.count) for m in bp.materials],
                         [("minecraft:gold_block", 6)])
        worlds = {(x, y, z) for x, y, z, _b in bp.iter_blocks()}
        self.assertEqual(worlds, {(5, 2, -7), (6, 2, -7), (5, 3, -7),
                                  (6, 3, -7), (5, 4, -7), (6, 4, -7)})
        self.assertTrue(any("负值" in n for n in bp.notes))


# ══════════════════════════════════════════════════════════════════════
# 4. 版本 / 格式：如实拒绝
# ══════════════════════════════════════════════════════════════════════

class VersionGateTests(_TempMixin):
    def test_unknown_litematic_format_version_rejected_with_number(self):
        raw = litematic({"r": _region("r", (0, 0, 0), (1, 1, 1),
                                      [("minecraft:stone", {})], [0])}, version=99)
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(self.write("v99.litematic", raw))
        self.assertEqual(ctx.exception.code, "unsupported_version")
        message = str(ctx.exception)
        self.assertIn("99", message)
        self.assertIn("1, 2, 3, 4, 5, 6, 7", message)   # "我认识的范围"

    def test_unknown_data_version_rejected_with_number(self):
        raw = litematic({"r": _region("r", (0, 0, 0), (1, 1, 1),
                                      [("minecraft:stone", {})], [0])}, data_version=42)
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(self.write("dv.litematic", raw))
        self.assertEqual(ctx.exception.code, "unsupported_version")
        self.assertIn("42", str(ctx.exception))
        self.assertIn("1451", str(ctx.exception))

    def test_unknown_sponge_version_rejected_with_number(self):
        raw = _root({"Version": I(7), "DataVersion": I(4189),
                     "Width": SH(1), "Height": SH(1), "Length": SH(1),
                     "Palette": COMPOUND({"minecraft:stone": I(0)}),
                     "BlockData": BYTE_ARRAY(b"\x00")})
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(self.write("s7.schem", raw))
        self.assertEqual(ctx.exception.code, "unsupported_version")
        self.assertIn("7", str(ctx.exception))
        self.assertIn("2, 3", str(ctx.exception))

    def test_legacy_schematic_rejected(self):
        raw = _root({"Width": SH(2), "Height": SH(1), "Length": SH(1),
                     "Materials": STR("Alpha"),
                     "Blocks": BYTE_ARRAY(b"\x01\x02"),
                     "Data": BYTE_ARRAY(b"\x00\x00")})
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(self.write("old.schematic", raw))
        self.assertEqual(ctx.exception.code, "legacy_schematic")
        self.assertIn(".schem", str(ctx.exception))

    def test_unknown_root_rejected(self):
        raw = _root({"Hello": I(1)})
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(self.write("x.nbt", raw))
        self.assertEqual(ctx.exception.code, "unsupported_format")

    def test_not_nbt_and_missing_file(self):
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(self.write("junk.nbt", b"not an nbt at all"))
        self.assertEqual(ctx.exception.code, "not_nbt")
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(self.dir / "nope.litematic")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_missing_version_with_dimensions_rejected(self):
        raw = _root({"Width": SH(1), "Height": SH(1), "Length": SH(1)})
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(self.write("nv.schem", raw))
        self.assertEqual(ctx.exception.code, "missing_field")


# ══════════════════════════════════════════════════════════════════════
# 5. 上限（有界行为：报错，不崩、不 OOM）
# ══════════════════════════════════════════════════════════════════════

class LimitTests(_TempMixin):
    def test_file_too_large(self):
        raw = litematic({"r": _region("r", (0, 0, 0), (1, 1, 1),
                                      [("minecraft:stone", {})], [0])})
        path = self.write("big.litematic", raw)
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(path, max_file_bytes=8)
        self.assertEqual(ctx.exception.code, "too_large")
        self.assertIn("超过上限", str(ctx.exception))

    def test_too_many_regions(self):
        regions = {f"r{i}": _region(f"r{i}", (i, 0, 0), (1, 1, 1),
                                    [("minecraft:stone", {})], [0]) for i in range(3)}
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(self.write("many.litematic", litematic(regions)), max_regions=2)
        self.assertEqual(ctx.exception.code, "too_many_regions")

    def test_too_many_blocks(self):
        raw = litematic({"r": _region("r", (0, 0, 0), (8, 8, 8),
                                      [("minecraft:stone", {})], [0] * 512)})
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(self.write("huge.litematic", raw), max_blocks=100)
        self.assertEqual(ctx.exception.code, "too_many_blocks")

    def test_decompression_bomb_capped(self):
        raw = litematic({"r": _region("r", (0, 0, 0), (1, 1, 1),
                                      [("minecraft:stone", {})], [0])})
        with self.assertRaises(BlueprintError) as ctx:
            parse_blueprint_bytes(raw, max_decompressed_bytes=16)
        self.assertEqual(ctx.exception.code, "too_large")

    def test_truncated_gzip_rejected(self):
        raw = litematic({"r": _region("r", (0, 0, 0), (1, 1, 1),
                                      [("minecraft:stone", {})], [0])})
        with self.assertRaises(BlueprintError) as ctx:
            parse_blueprint_bytes(raw[:len(raw) // 2])
        self.assertEqual(ctx.exception.code, "bad_compression")

    def test_uncompressed_nbt_also_read(self):
        raw = litematic({"r": _region("r", (0, 0, 0), (1, 1, 1),
                                      [("minecraft:stone", {})], [0])}, compress="none")
        bp = parse_blueprint_bytes(raw)
        self.assertEqual(bp.total_volume, 1)

    def test_zlib_compressed_nbt_also_read(self):
        raw = litematic({"r": _region("r", (0, 0, 0), (1, 1, 1),
                                      [("minecraft:stone", {})], [0])}, compress="zlib")
        bp = parse_blueprint_bytes(raw)
        self.assertEqual(bp.total_volume, 1)


# ══════════════════════════════════════════════════════════════════════
# 6. Sponge v2 / v3
# ══════════════════════════════════════════════════════════════════════

class SpongeTests(_TempMixin):
    def test_v2_basic_and_index_order(self):
        palette = {"minecraft:stone": 0, "minecraft:oak_planks": 1, "minecraft:air": 2}
        width, height, length = 2, 1, 2
        ids = [2] * (width * height * length)
        ids[_index((width, height, length), 1, 0, 0)] = 0
        ids[_index((width, height, length), 0, 0, 1)] = 1
        raw = sponge(width, height, length, palette, ids, version=2,
                     we_offset=(-10, 64, 3))
        bp = load_blueprint(self.write("v2.schem", raw))
        self.assertEqual(bp.format, "sponge")
        self.assertEqual(bp.format_version, 2)
        self.assertEqual(bp.size, (2, 1, 2))
        self.assertEqual(bp.origin, (-10, 64, 3))       # v2：取自 Metadata.WEOffset*
        self.assertEqual(bp.total_volume, 4)
        self.assertEqual(bp.air_blocks, 2)
        self.assertEqual([(m.block, m.count) for m in bp.materials],
                         [("minecraft:oak_planks", 1), ("minecraft:stone", 1)])
        self.assertEqual(set(bp.iter_blocks(include_air=False)), {
            (-9, 64, 3, "minecraft:stone"),
            (-10, 64, 4, "minecraft:oak_planks"),
        })

    def test_v2_offset_falls_back_to_root_offset(self):
        palette = {"minecraft:stone": 0}
        raw = sponge(1, 1, 1, palette, [0], version=2, offset=[5, 6, 7])
        bp = load_blueprint(self.write("v2b.schem", raw))
        self.assertEqual(bp.origin, (5, 6, 7))

    def test_v2_multibyte_varint_index(self):
        # 130 项调色板 ⇒ 索引 129 需要 2 字节 varint
        palette = {f"minecraft:block_{i:03d}": i for i in range(130)}
        width, height, length = 3, 1, 1
        raw = sponge(width, height, length, palette, [0, 129, 128], version=2)
        bp = load_blueprint(self.write("v2c.schem", raw))
        self.assertEqual([b for *_c, b in bp.iter_blocks()],
                         ["minecraft:block_000", "minecraft:block_129",
                          "minecraft:block_128"])

    def test_v3_blocks_compound_and_offset(self):
        palette = {"minecraft:polished_andesite": 0, "minecraft:air": 1}
        width, height, length = 2, 1, 1
        raw = sponge(width, height, length, palette, [1, 0], version=3,
                     offset=[100, 70, -20],
                     block_entities=[{"Id": STR("minecraft:chest"),
                                      "Pos": INT_ARRAY([1, 0, 0])}])
        bp = load_blueprint(self.write("v3.schem", raw))
        self.assertEqual(bp.format_version, 3)
        self.assertEqual(bp.origin, (100, 70, -20))
        self.assertEqual([(m.block, m.count) for m in bp.materials],
                         [("minecraft:polished_andesite", 1)])
        self.assertEqual(len(bp.block_entities), 1)
        self.assertEqual(set(bp.iter_blocks(include_air=False)),
                         {(101, 70, -20, "minecraft:polished_andesite")})

    def test_v3_nested_schematic_wrapper(self):
        palette = {"minecraft:stone": 0}
        raw = sponge(1, 1, 1, palette, [0], version=3, offset=[1, 2, 3], nested=True)
        bp = load_blueprint(self.write("v3n.schem", raw))
        self.assertEqual(bp.layout, "schematic-wrapper")
        self.assertEqual(bp.origin, (1, 2, 3))

    def test_v3_without_blocks_falls_back_to_root_and_says_so(self):
        # version=3 但把 Palette/Data 放在根上（少数写入器）
        fields = {"Version": I(3), "DataVersion": I(4189),
                  "Width": SH(1), "Height": SH(1), "Length": SH(1),
                  "Palette": COMPOUND({"minecraft:stone": I(0)}),
                  "BlockData": BYTE_ARRAY(b"\x00")}
        bp = load_blueprint(self.write("v3r.schem", _root(fields)))
        self.assertIn("root-blocks", bp.layout)
        self.assertTrue(any("Blocks" in n for n in bp.notes))

    def test_truncated_blockdata_rejected(self):
        palette = {"minecraft:stone": 0, "minecraft:dirt": 1}
        fields = {"Version": I(2), "DataVersion": I(4189),
                  "Width": SH(2), "Height": SH(1), "Length": SH(1),
                  "Palette": COMPOUND({k: I(v) for k, v in palette.items()}),
                  "BlockData": BYTE_ARRAY(b"\x00")}   # 只够 1 个
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(self.write("short.schem", _root(fields)))
        self.assertEqual(ctx.exception.code, "truncated")

    def test_palette_index_out_of_range_rejected(self):
        fields = {"Version": I(2), "DataVersion": I(4189),
                  "Width": SH(1), "Height": SH(1), "Length": SH(1),
                  "Palette": COMPOUND({"minecraft:stone": I(0)}),
                  "BlockData": BYTE_ARRAY(b"\x05")}
        with self.assertRaises(BlueprintError) as ctx:
            load_blueprint(self.write("bad.schem", _root(fields)))
        self.assertEqual(ctx.exception.code, "bad_palette")

    def test_air_only_sponge(self):
        palette = {"minecraft:air": 0}
        bp = load_blueprint(self.write("air.schem", sponge(2, 2, 2, palette, [0] * 8)))
        self.assertEqual(bp.materials, ())
        self.assertEqual(bp.air_blocks, 8)
        self.assertIn("只有空气", render_summary(bp))


# ══════════════════════════════════════════════════════════════════════
# 7. 摘要渲染（给模型用；不许塞逐块数据）
# ══════════════════════════════════════════════════════════════════════

class RenderSummaryTests(_TempMixin):
    def _big(self):
        # 20 种材料、每种若干格，故意超过 top_materials=15
        palette = [(f"minecraft:block_{i:02d}", {}) for i in range(20)]
        size = (20, 1, 2)
        values = [i % 20 for i in range(40)]
        raw = litematic({"r": _region("r", (0, 0, 0), size, palette, values)})
        return load_blueprint(self.write("big.litematic", raw))

    def test_zh_summary_shape_and_paging_hint(self):
        bp = self._big()
        text = render_summary(bp)
        self.assertIn("【蓝图】", text)
        self.assertIn("尺寸 20×1×2", text)
        self.assertIn("要准备的材料", text)
        self.assertIn("blueprint_materials", text)      # 超长时指路分页
        self.assertIn("…还有", text)
        # 只给计数，绝不逐块罗列
        self.assertLess(len(text), 3000)
        self.assertNotIn("20,0,0", text)

    def test_en_summary(self):
        bp = self._big()
        text = render_summary(bp, lang="en")
        self.assertIn("[Blueprint]", text)
        self.assertIn("Materials to gather", text)
        self.assertIn("more (use blueprint_materials", text)

    def test_bad_lang_rejected(self):
        with self.assertRaises(ValueError):
            render_summary(self._big(), lang="fr")

    def test_materials_page(self):
        bp = self._big()
        page = bp.materials_page(offset=0, limit=3)
        self.assertEqual(len(page), 3)
        self.assertEqual(page[0]["count"], 2)
        self.assertIn("tags", page[0])

    def test_values_are_thousands_separated(self):
        palette = [("minecraft:stone", {})]
        raw = litematic({"r": _region("r", (0, 0, 0), (10, 10, 10), palette, [0] * 1000)})
        bp = load_blueprint(self.write("k.litematic", raw))
        self.assertIn("1,000", render_summary(bp))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
