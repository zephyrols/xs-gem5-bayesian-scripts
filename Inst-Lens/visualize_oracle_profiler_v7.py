"""
InstLens — Oracle Profiler Visualizer

将 GEM5 oracle_profiler CSV 中的指令级阻塞周期归因可视化为
交互式堆叠条形图与饼图（Plotly HTML）。

架构概览：
  CSV  ──parse──▶  CheckpointData  ──aggregate──▶  AggregatedView  ──render──▶  HTML
                   (per checkpoint)    (weighted)     (per application)

聚合策略：
  checkpoint → workload:    SimPoint 权重加权平均（normalize=True）
  workload   → application: 直接求和（normalize=False）

用法：
  python3 instlens.py -i /path/to/checkpoints -o out_plots -j 8
"""

from __future__ import annotations

import argparse
import colorsys
import json
import logging
import os
import re
import textwrap
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(it, **_kw):  # noqa: D103
        return it
    tqdm.write = lambda msg, **_kw: print(msg)  # type: ignore[attr-defined]


# ═══════════════════════════════════════════════════════════════════════════
# §1  Logging
# ═══════════════════════════════════════════════════════════════════════════

LOG_NAME = "InstLens"


class _TqdmHandler(logging.Handler):
    """通过 tqdm.write 输出日志，避免破坏进度条。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            tqdm.write(msg)
        except Exception:
            self.handleError(record)


def _init_logger(verbose: bool = False) -> logging.Logger:
    log = logging.getLogger(LOG_NAME)
    if not log.handlers:
        h = _TqdmHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s  %(message)s"))
        log.addHandler(h)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    return log


log = logging.getLogger(LOG_NAME)


# ═══════════════════════════════════════════════════════════════════════════
# §2  Pipeline Stage Taxonomy
# ═══════════════════════════════════════════════════════════════════════════

# ── L2: raw column → pipeline stage ──

_L2_RULES: List[Tuple[str, str]] = [
    ("^pipeline", "Pipeline"),  # Pipeline_* 级间延迟（独立分类）
    ("^retire",   "Base"),      # Retire 归入 Base
    ("base",      "Base"),
    ("fetch",     "Fetch"),
    ("decode",    "Decode"),
    ("rename",    "Rename"),
    ("dispatch",  "Dispatch"),
    ("issue",     "Issue"),
    ("execute",   "Execute"),
    ("writeback", "WriteBack"),
    ("commit",    "Commit"),
    ("serialize", "Serialize"),
    ("handle",    "INT/EXC"),
    ("squash",    "Squash"),
    ("unknown",   "Other"),
]

_L2_TO_L1: Dict[str, str] = {
    "Base": "Base",          
    "Pipeline": "Pipeline",
    "Fetch": "Front-End",   
    "Decode": "Front-End",
    "Rename": "Back-End",   
    "Dispatch": "Back-End",
    "Issue": "Back-End",   
     "Execute": "Back-End",
    "WriteBack": "Back-End",  
    "Commit": "Back-End",
    "Squash": "Squash",
    "Serialize": "Serialize",   
    "INT/EXC": "INT/EXC",
    "Other": "Other",
}

# ── L3: Execute 子单元聚合 ──

def _classify_l3(col: str) -> str:
    """L3: 所有 Execute_LSU_* 聚合为 Execute_LSU，其余不变。"""
    if col.startswith("Execute_LSU"):
        return "Execute_LSU"
    return col

# ── L4: Execute_LSU 内部子聚合 ──

_L4_RULES: List[Tuple[str, str]] = [
    (r"Execute_LSU_(?:Load|Store|Atomic)_L1Cache", "Execute_LSU_L1Cache"),
    (r"Execute_LSU_(?:Load|Store|Atomic)_L2Cache", "Execute_LSU_L2Cache"),
    (r"Execute_LSU_(?:Load|Store|Atomic)_L3Cache", "Execute_LSU_L3Cache"),
    (r"Execute_LSU_(?:Load|Store|Atomic)_Mem",     "Execute_LSU_Mem"),
    (r"Execute_LSU_Replay_",                        "Execute_LSU_Replay"),
]


def _classify_l2(col: str) -> str:
    low = col.lower()
    return next((name for pat, name in _L2_RULES if re.search(pat, low)), "Other")


def _classify_l4(col: str) -> str:
    """L4: 对 Execute_LSU_* 做 Load/Store/Atomic 合并，其余保持 identity。"""
    return next((name for pat, name in _L4_RULES if re.search(pat, col)), col)


@dataclass(frozen=True)
class StageMappings:
    """五层映射关系：col → L4 → L3 → L2 → L1, col → col (identity=L5)。"""
    l1: Dict[str, str]
    l2: Dict[str, str]
    l3: Dict[str, str]
    l4: Dict[str, str]
    l5: Dict[str, str]

    @classmethod
    def from_columns(cls, columns: Sequence[str]) -> StageMappings:
        l2 = {c: _classify_l2(c) for c in columns}
        l1 = {c: _L2_TO_L1.get(g, "Other") for c, g in l2.items()}
        l3 = {c: _classify_l3(c) for c in columns}
        l4 = {c: _classify_l4(c) for c in columns}
        l5 = {c: c for c in columns}
        return cls(l1=l1, l2=l2, l3=l3, l4=l4, l5=l5)

    def for_level(self, level: int) -> Dict[str, str]:
        return {1: self.l1, 2: self.l2, 3: self.l3, 4: self.l4, 5: self.l5}[level]


# ═══════════════════════════════════════════════════════════════════════════
# §2b  Color Palette — 学术配色 + 族内高区分度
# ═══════════════════════════════════════════════════════════════════════════
#
#   设计原则：
#     1. 美观 — 饱和度 30-52%，参考 Tableau-10 / Paul Tol 色觉友好
#        方案，比纯灰调更有辨识度，同时不至于"荧光信息图"。
#     2. 灰度可辨 — 各 L2 阶段灰度亮度等距 (100 → 248)，
#        相邻间隔 ≥ 8，黑白打印可分辨。
#     3. 族内高区分 — 同族成员在明度梯度上叠加色相微旋转 (±12°)
#        与饱和度渐变，即使 Execute 有 23 个子项也能逐个辨认。
#
#   亮度校准表：
#     INT/EXC 100  Commit 112  Squash 126  Execute 140  WriteBack 154
#     Rename 168   Other 178   Fetch 188   Decode 198   Pipeline 208
#     Serialize 218  Dispatch 228  Issue 238  Base 248

# (hue°, saturation%, lightness%)
_COLOR_BASE_HSL: Dict[str, Tuple[int, int, int]] = {
    # ── L1 类别 ──
    "Front-End":     (210, 48, 76),   # lum≈188  钴蓝
    "Back-End":      ( 12, 52, 59),   # lum≈140  赤陶
    "Pipeline":      (205, 30, 82),   # lum≈208  钢蓝
    "Squash":        ( 28, 35, 47),   # lum≈126  棕褐

    # ── L2 类别 ──
    "Base":          (  0,  0, 96),   # lum≈248  极浅灰
    "Pipeline":      (205, 30, 82),   # lum≈208  钢蓝
    "Fetch":         (210, 48, 76),   # lum≈188  钴蓝
    "Decode":        (175, 42, 73),   # lum≈198  青绿
    "Rename":        (145, 42, 62),   # lum≈168  翠绿
    "Dispatch":      ( 90, 38, 87),   # lum≈228  黄绿
    "Issue":         ( 40, 50, 92),   # lum≈238  琥珀
    "Execute":       ( 12, 52, 59),   # lum≈140  赤陶（L2 聚合色）
    "WriteBack":     (272, 38, 66),   # lum≈154  薰衣草
    "Commit":        (228, 42, 54),   # lum≈112  靛蓝
    "Squash":        ( 28, 35, 47),   # lum≈126  棕褐
    "Serialize":     ( 65, 32, 81),   # lum≈218  苔绿
    "INT/EXC":       (348, 42, 46),   # lum≈100  玫红
    "Other":         (  0,  0, 70),   # lum≈178  中灰

    # ── L3 类别（Execute 子单元独立配色） ──
    "Execute_LSU":       (  8, 52, 57),   # 赤陶红 — LSU 最大子族 (20 members)
    "Execute_ScalarALU": ( 38, 50, 58),   # 琥珀棕
    "Execute_VectorALU": (328, 40, 58),   # 玫瑰紫
    "Execute_FPU":       (195, 42, 52),   # 青蓝
}


def _color_parent(group_name: str) -> str:
    """确定某个 group 名称所属的配色族。

    查找顺序：直接匹配 → L3 分类 → L2 分类。
    这使得 Execute 的四个 L3 子单元（LSU / ScalarALU / VectorALU / FPU）
    各自拥有独立色系，而其他阶段仍回退到 L2 配色。
    """
    if group_name in _COLOR_BASE_HSL:
        return group_name
    l3 = _classify_l3(group_name)
    if l3 in _COLOR_BASE_HSL:
        return l3
    return _classify_l2(group_name)


def _hsl_to_hex(h: float, s: float, l: float) -> str:
    """HSL (h: 0-360, s: 0-100, l: 0-100) → #RRGGBB。"""
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, l / 100.0, s / 100.0)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"


def _assign_colors(group_names: Sequence[str]) -> Dict[str, str]:
    """为一组 group 名称分配颜色，同 L2 父级使用同色系。

    族内区分策略（三轴渐变）：
      - 明度 L：暗 → 亮，spread 随成员数自适应
      - 色相 H：±spread_h 微旋转（最多 ±12°），让相邻成员
        不仅有深浅差异，还有冷暖色调差异
      - 饱和度 S：暗端浓 → 亮端淡，维持视觉平衡

    颜色稳定性：输入列集合固定 → group 集合固定 → 排序固定 →
    每个 group 位置固定 → 颜色恒定。
    """
    buckets: Dict[str, List[str]] = {}
    for g in group_names:
        parent = _color_parent(g)
        buckets.setdefault(parent, []).append(g)

    colors: Dict[str, str] = {}
    for parent, members in buckets.items():
        h_base, s_base, l_base = _COLOR_BASE_HSL.get(parent, (0, 0, 70))
        members_sorted = sorted(members)
        n = len(members_sorted)

        if n == 1:
            colors[members_sorted[0]] = _hsl_to_hex(h_base, s_base, l_base)
            continue

        # 三轴展开幅度
        spread_l = min(25, 6 + n * 2)          # 明度半幅
        spread_h = min(12, 3 + n * 0.4)        # 色相半幅（°）

        for i, m in enumerate(members_sorted):
            t = i / (n - 1)                      # 0 → 1
            h = h_base - spread_h + 2 * spread_h * t   # 色相旋转
            l = l_base - spread_l + 2 * spread_l * t    # 明度梯度
            s = s_base + 10 - 20 * t                    # 饱和度渐变
            l = max(35, min(90, l))
            s = max(25, min(62, s))
            colors[m] = _hsl_to_hex(h, s, l)

    return colors


# ═══════════════════════════════════════════════════════════════════════════
# §3  Data Model
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class LevelData:
    """单个聚合层级的数据：DataFrame (index=PC, columns=groups) + 列名列表。"""
    df: pd.DataFrame
    cols: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.cols:
            self.cols = self.df.columns.tolist()


@dataclass
class ProfileData:
    """一个分析单元（checkpoint / workload / application）的完整画像。"""
    levels: Dict[int, LevelData]          # {1: ..., 2: ..., 3: ..., 4: ..., 5: ...}
    pcs: List[int]
    counts: pd.Series                      # index = PC
    disasms: pd.Series                     # index = PC
    weight: float = 0.0

    def level_df(self, level: int) -> pd.DataFrame:
        return self.levels[level].df

    def level_cols(self, level: int) -> List[str]:
        return self.levels[level].cols

    @property
    def disasm_dict(self) -> Dict[int, str]:
        return self.disasms.to_dict()


# ═══════════════════════════════════════════════════════════════════════════
# §4  CSV Parsing
# ═══════════════════════════════════════════════════════════════════════════

def _read_csv(path: str) -> pd.DataFrame:
    kw = dict(quotechar='"', encoding="utf-8", skipinitialspace=True)
    try:
        return pd.read_csv(path, engine="python", **kw)
    except Exception:
        return pd.read_csv(path, engine="c", on_bad_lines="skip", **kw)


def _parse_pc(v) -> int:
    if pd.isna(v):
        return 0
    if isinstance(v, (int, np.integer)):
        return int(v)
    s = str(v).strip()
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(float(s))
    except Exception:
        return 0


def _numeric_block(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    """向量化数值清洗：去引号 / 空白 / 千位逗号 → float。"""
    s = df[cols].astype(str)
    for c in s.columns:
        s[c] = (s[c].str.strip()
                     .str.strip("\"'")
                     .str.replace("\u00a0", "", regex=False)
                     .str.replace(" ", "", regex=False)
                     .str.replace("−", "-", regex=False)
                     .str.replace(",", "", regex=False))
    return s.apply(pd.to_numeric, errors="coerce").fillna(0)


def _aggregate_groups(vals: pd.DataFrame, mapping: Dict[str, str]) -> pd.DataFrame:
    """按 mapping 对列分组求和。"""
    buckets: Dict[str, List[str]] = {}
    for col, grp in mapping.items():
        if col in vals.columns:
            buckets.setdefault(grp, []).append(col)
    return pd.concat(
        {grp: vals[cs].sum(axis=1) for grp, cs in buckets.items()},
        axis=1,
    )


def parse_checkpoint(path: str, weight: float = 0.0) -> Optional[ProfileData]:
    """解析单个 checkpoint CSV → ProfileData。"""
    df = _read_csv(path)
    df = df.rename(columns=str.strip)
    df.columns = [c.strip('"') for c in df.columns]

    required = {"PC", "Disassembly", "Count"}
    if not required.issubset(df.columns):
        raise RuntimeError(f"CSV 缺少必要列 {required - set(df.columns)}")

    cycle_cols = [c for c in df.columns if c not in required]
    if not cycle_cols:
        raise RuntimeError("缺少阻塞时钟归因列")

    vals = _numeric_block(df, cycle_cols)
    meta = df[["PC", "Disassembly", "Count"]].copy()

    mask = meta["PC"].map(_parse_pc).ne(0)
    meta, vals = meta.loc[mask].reset_index(drop=True), vals.loc[mask].reset_index(drop=True)

    pc_idx = meta["PC"].map(_parse_pc).tolist()
    mappings = StageMappings.from_columns(cycle_cols)

    levels: Dict[int, LevelData] = {}
    for lv in (1, 2, 3, 4, 5):
        agg = _aggregate_groups(vals, mappings.for_level(lv))
        agg.index = pc_idx
        levels[lv] = LevelData(df=agg)

    counts = pd.to_numeric(meta["Count"], errors="coerce").fillna(0)
    counts.index = pc_idx
    disasms = meta["Disassembly"].astype(str)
    disasms.index = pc_idx

    return ProfileData(levels=levels, pcs=pc_idx, counts=counts,
                       disasms=disasms, weight=weight)


# ═══════════════════════════════════════════════════════════════════════════
# §5  Weighted Aggregation
# ═══════════════════════════════════════════════════════════════════════════

def _union_cols(sources: Sequence[ProfileData], level: int) -> List[str]:
    seen: set = set()
    for s in sources:
        if level in s.levels:
            seen.update(s.level_cols(level))
    return sorted(seen)


def aggregate_profiles(
    sources: Sequence[ProfileData],
    total_weight: float,
    *,
    normalize: bool = True,
) -> ProfileData:
    """将多个 ProfileData 合并为一个。"""
    all_pcs: set = set()
    all_disasms: Dict[int, str] = {}
    for s in sources:
        all_pcs.update(s.pcs)
        all_disasms.update(s.disasm_dict)
    pc_list = sorted(all_pcs)
    idx = pd.Index(pc_list)

    levels: Dict[int, LevelData] = {}
    count_accum = pd.Series(0.0, index=idx)

    for lv in (1, 2, 3, 4, 5):
        cols = _union_cols(sources, lv)
        if not cols:
            continue
        accum = pd.DataFrame(0.0, index=idx, columns=cols)
        for s in sources:
            if lv not in s.levels or (normalize and s.weight == 0):
                continue
            w = (s.weight / total_weight) if normalize else 1.0
            aligned = s.level_df(lv).reindex(index=idx, columns=cols, fill_value=0.0)
            accum += aligned * w
            if lv == 1:
                count_accum += s.counts.reindex(idx, fill_value=0.0) * w
        levels[lv] = LevelData(df=accum)

    disasm_series = pd.Series(all_disasms).reindex(idx, fill_value="")
    return ProfileData(levels=levels, pcs=pc_list, counts=count_accum,
                       disasms=disasm_series, weight=total_weight)


def _group_and_aggregate(
    groups: Dict[str, List[str]],
    source_map: Dict[str, ProfileData],
    weight_fn,
    desc: str = "",
    normalize: bool = True,
) -> Dict[str, ProfileData]:
    """通用的分组聚合入口。"""
    result: Dict[str, ProfileData] = {}
    for target, src_keys in tqdm(groups.items(), desc=f"Aggregate {desc}", unit="grp"):
        sources = [source_map[k] for k in src_keys if k in source_map]
        tw = sum(weight_fn(k) for k in src_keys)
        if not sources or (normalize and tw == 0):
            continue
        result[target] = aggregate_profiles(sources, tw, normalize=normalize)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# §6  Labels
# ═══════════════════════════════════════════════════════════════════════════

def _make_label(pc: int, dis: str) -> str:
    return f"(0x{pc:08x}, {dis.replace(chr(34), '')})"


def _build_labels(profile: ProfileData, wrap_width: int = 80) -> List[str]:
    dd = profile.disasm_dict
    raw = [_make_label(pc, dd.get(pc, "")) for pc in profile.pcs]
    return [textwrap.fill(l, width=wrap_width) for l in raw]


# ═══════════════════════════════════════════════════════════════════════════
# §7  Plotly Renderers
# ═══════════════════════════════════════════════════════════════════════════

def _is_base(col: str) -> bool:
    return col.startswith("Base")


# ── 7a  Combined Stacked Bar (all applications) ─────────────────────────

def _round_list(arr, ndigits: int = 2) -> list:
    return [round(float(v), ndigits) for v in arr]


def _build_bar_app_data(
    profile: ProfileData,
    labels: List[str],
    max_top: int = 50,
) -> dict:
    level_keys = sorted(profile.levels)
    states: dict = {}

    for lv in level_keys:
        df = profile.level_df(lv)
        groups = df.columns.tolist()
        base_set = {g for g in groups if _is_base(g)}
        non_base = [g for g in groups if g not in base_set]

        tot_inc = df.sum(axis=1).values
        tot_exc = df[non_base].sum(axis=1).values if non_base else tot_inc

        for inc, tag, order in [
            (True,  "1", np.argsort(-tot_inc)),
            (False, "0", np.argsort(-tot_exc)),
        ]:
            top_order = order[:max_top]
            active = groups if inc else non_base
            states[f"{tag}_{lv}"] = {
                "x": [labels[i] for i in top_order],
                "g": active,
                "y": [_round_list(df[g].values[top_order]) for g in active],
            }

    return {"s": states}


def _collect_all_groups(all_data: Dict[str, dict]) -> List[str]:
    """从所有 app 数据中收集全部 group 名称。"""
    seen: set = set()
    for app_data in all_data.values():
        for state_data in app_data["s"].values():
            seen.update(state_data["g"])
    return sorted(seen)


def render_all_stacked_bar(
    app_profiles: Dict[str, Tuple[ProfileData, List[str]]],
    outpath: str,
    *,
    title: str = "",
    top_options: Sequence[int] = (10, 20, 30, 50),
):
    """所有 application 合并到一个交互式堆叠条形图 HTML。"""
    app_names = sorted(app_profiles.keys())
    all_data: Dict[str, dict] = {}
    level_keys_set: set = set()
    max_top = max(top_options)

    for app_name in app_names:
        profile, labels = app_profiles[app_name]
        all_data[app_name] = _build_bar_app_data(profile, labels, max_top)
        level_keys_set.update(profile.levels.keys())

    level_keys = sorted(level_keys_set)
    default_app = app_names[0]
    default_lv = 2 if 2 in level_keys else level_keys[0]
    default_top = top_options[0]

    # ── 生成全局色彩映射 ──
    all_groups = _collect_all_groups(all_data)
    color_map = _assign_colors(all_groups)

    app_opts = "".join(f'<option value="{a}">{a}</option>' for a in app_names)
    lv_opts = "".join(f'<option value="{v}">Level {v}</option>' for v in level_keys)
    top_opts = "".join(f'<option value="{v}">Top {v}</option>' for v in top_options)

    html = _ALL_STACKED_BAR_TEMPLATE.format(
        title=title or "All Applications — Stacked Bar",
        app_options=app_opts,
        level_options=lv_opts,
        top_options=top_opts,
        all_data_json=json.dumps(all_data, ensure_ascii=False, separators=(",", ":")),
        color_map_json=json.dumps(color_map, ensure_ascii=False, separators=(",", ":")),
        default_app=default_app,
        default_inc="0",
        default_lv=default_lv,
        default_top=default_top,
    )
    _write(outpath, html)


_ALL_STACKED_BAR_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{title}</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 16px; }}
  .ctl {{ display: flex; gap: 16px; align-items: center; margin-bottom: 8px;
          flex-wrap: wrap; }}
  .ctl label {{ font-size: 14px; }}
  .ctl select {{ padding: 2px 6px; }}
  #gd {{ width: 100%; height: calc(100vh - 80px); min-height: 600px; }}
</style></head><body>
<div class="ctl">
  <label>App:   <select id="app">{app_options}</select></label>
  <label>Base:  <select id="inc">
    <option value="1">Include</option><option value="0">Exclude</option>
  </select></label>
  <label>Level: <select id="lv">{level_options}</select></label>
  <label>Top:   <select id="tn">{top_options}</select></label>
</div>
<div id="gd"></div>
<script>
var D={all_data_json};
var CM={color_map_json};
var ap=document.getElementById('app'),
    inc=document.getElementById('inc'),
    lv=document.getElementById('lv'),
    tn=document.getElementById('tn'),
    gd=document.getElementById('gd');
ap.value='{default_app}'; inc.value='{default_inc}';
lv.value='{default_lv}'; tn.value='{default_top}';
function go(){{
  var ad=D[ap.value]; if(!ad) return;
  var st=ad.s[inc.value+'_'+lv.value]; if(!st) return;
  var top=parseInt(tn.value);
  var xs=st.x.slice(0,top);
  var traces=st.g.map(function(g,gi){{
    return {{
      name:g, type:'bar', x:xs,
      y:st.y[gi].slice(0,top).map(function(v){{return v===0?null:v;}}),
      marker:{{color:CM[g]||'#888888'}}
    }};
  }});
  Plotly.react(gd,traces,{{
    barmode:'stack',
    title:{{text:'Application: '+ap.value,xanchor:'center',yanchor:'top'}},
    xaxis:{{tickangle:-45,automargin:true}},
    yaxis:{{automargin:true}},
    legend:{{traceorder:'normal',orientation:'h',x:0.5,xanchor:'center',y:-0.25,yanchor:'top'}},
    hoverlabel:{{namelength:-1}},
    hovermode:'x',
    margin:{{t:60,b:200,l:60,r:30}},
    autosize:true
  }},{{responsive:true}});
}}
ap.onchange=inc.onchange=lv.onchange=tn.onchange=go; go();
</script></body></html>"""


# ── 7b  Combined Pie (all applications) ─────────────────────────────────

def _build_pie_app_data(profile: ProfileData) -> dict:
    level_keys = sorted(profile.levels)
    data: dict = {}
    for lv in level_keys:
        totals = profile.level_df(lv).sum().sort_values(ascending=False)

        inc_labels = totals.index.tolist()
        inc_values = _round_list(totals.values)
        data[f"1_{lv}"] = {"l": inc_labels, "v": inc_values}

        exc_mask = [not _is_base(c) for c in totals.index]
        exc_labels = [l for l, m in zip(totals.index, exc_mask) if m]
        exc_values = _round_list(v for v, m in zip(totals.values, exc_mask) if m)
        data[f"0_{lv}"] = {"l": exc_labels, "v": exc_values}

    return data


def render_all_pie(
    app_profiles: Dict[str, ProfileData],
    outpath: str,
    *,
    title: str = "",
    default_level: int = 2,
):
    """所有 application 合并到一个交互式饼图 HTML。"""
    app_names = sorted(app_profiles.keys())
    all_data: Dict[str, dict] = {}
    level_keys_set: set = set()

    for app_name in app_names:
        profile = app_profiles[app_name]
        all_data[app_name] = _build_pie_app_data(profile)
        level_keys_set.update(profile.levels.keys())

    level_keys = sorted(level_keys_set)
    if default_level not in level_keys:
        default_level = level_keys[0]

    # ── 收集所有 group 名称并生成色彩映射 ──
    all_groups: set = set()
    for app_data in all_data.values():
        for state_data in app_data.values():
            all_groups.update(state_data["l"])
    color_map = _assign_colors(sorted(all_groups))

    default_app = app_names[0]
    app_opts = "".join(f'<option value="{a}">{a}</option>' for a in app_names)
    lv_opts = "".join(f'<option value="{v}">Level {v}</option>' for v in level_keys)

    html = _ALL_PIE_TEMPLATE.format(
        title=title or "All Applications — Pie",
        app_options=app_opts,
        level_options=lv_opts,
        all_data_json=json.dumps(all_data, ensure_ascii=False, separators=(",", ":")),
        color_map_json=json.dumps(color_map, ensure_ascii=False, separators=(",", ":")),
        default_app=default_app,
        default_inc="0",
        default_lv=default_level,
    )
    _write(outpath, html)


_ALL_PIE_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{title}</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 16px; }}
  .ctl {{ display: flex; gap: 16px; align-items: center; margin-bottom: 8px;
          flex-wrap: wrap; }}
  .ctl label {{ font-size: 14px; }}
  .ctl select {{ padding: 2px 6px; }}
  #gd {{ width: 100%; aspect-ratio: 5/4; min-height: 500px; max-height: 90vh; }}
</style></head><body>
<div class="ctl">
  <label>App:   <select id="app">{app_options}</select></label>
  <label>Base:  <select id="inc">
    <option value="1">Include</option><option value="0">Exclude</option>
  </select></label>
  <label>Level: <select id="lv">{level_options}</select></label>
</div>
<div id="gd"></div>
<script>
var D={all_data_json};
var CM={color_map_json};
var ap=document.getElementById('app'),
    inc=document.getElementById('inc'),
    lv=document.getElementById('lv'),
    gd=document.getElementById('gd');
ap.value='{default_app}'; inc.value='{default_inc}'; lv.value='{default_lv}';
function pctText(labels,vals){{
  var t=0; vals.forEach(function(v){{t+=v;}});
  return vals.map(function(v,i){{
    if(t<=0||v/t<0.01) return '';
    return labels[i]+'<br>'+(v/t*100).toFixed(1)+'%';
  }});
}}
function go(){{
  var ad=D[ap.value]; if(!ad) return;
  var s=ad[inc.value+'_'+lv.value]; if(!s) return;
  var colors=s.l.map(function(l){{return CM[l]||'#888888';}});
  var txt=pctText(s.l,s.v);
  Plotly.react(gd,[{{
    type:'pie',labels:s.l,values:s.v,text:txt,
    textposition:'auto',textinfo:'text',
    insidetextorientation:'horizontal',
    textfont:{{size:10,color:'#000'}},
    hoverinfo:'label+value+percent',
    marker:{{colors:colors,line:{{color:'#000',width:1.5}}}},
    domain:{{x:[0.05,0.65],y:[0.08,0.92]}}
  }}],{{
    title:{{text:ap.value+' — Cycle Breakdown',xanchor:'center',y:0.98}},
    showlegend:true,
    legend:{{x:0.72,y:0.5,yanchor:'middle',traceorder:'normal',font:{{size:12}}}},
    margin:{{t:50,b:60,l:40,r:10}},
    autosize:true
  }},{{responsive:true}});
}}
ap.onchange=inc.onchange=lv.onchange=go; go();
</script></body></html>"""


# ═══════════════════════════════════════════════════════════════════════════
# §8  I/O Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _write(path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def export_csv(profile: ProfileData, path: str):
    """将 ProfileData 导出为 CSV（格式与原始 oracle_profiler 一致）。

    列：PC, Disassembly, Count, <L5 stall reason columns...>
    行按总阻塞周期降序排列。
    """
    if 5 not in profile.levels:
        return

    l5 = profile.level_df(5)
    meta = pd.DataFrame({
        "PC": [f"0x{pc:08x}" for pc in profile.pcs],
        "Disassembly": profile.disasms.values,
        "Count": profile.counts.values,
    }, index=l5.index)

    out = pd.concat([meta, l5], axis=1)
    out = out.sort_index(ascending=True)
    out.to_csv(path, index=False, encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# §9  Parallel Checkpoint Loader
# ═══════════════════════════════════════════════════════════════════════════

def _load_one(cp: str, template: str) -> Optional[Tuple[str, ProfileData]]:
    path = template.format(cp)
    if not os.path.exists(path):
        return None
    cid = CheckpointId.parse(cp)
    try:
        return cp, parse_checkpoint(path, weight=cid.weight)
    except Exception as exc:
        logging.getLogger(LOG_NAME).warning("跳过 %s: %s", cp, exc)
        return None


def load_checkpoints(
    checkpoints: List[str],
    template: str,
    n_jobs: int = 0,
) -> Dict[str, ProfileData]:
    results: Dict[str, ProfileData] = {}
    cap = n_jobs if n_jobs > 0 else (os.cpu_count() or 1)
    n_workers = max(1, min(len(checkpoints), cap))
    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        futs = {pool.submit(_load_one, cp, template): cp for cp in checkpoints}
        for f in tqdm(as_completed(futs), total=len(futs), desc="Load checkpoints", unit="ckpt"):
            res = f.result()
            if res:
                results[res[0]] = res[1]
    log.info("加载成功 %d / %d", len(results), len(checkpoints))
    return results


# ═══════════════════════════════════════════════════════════════════════════
# §10  Checkpoint Naming Convention
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CheckpointId:
    raw:       str
    workload:  str
    app:       str
    simpoint:  int
    weight:    float

    @classmethod
    def parse(cls, name: str) -> CheckpointId:
        parts = name.rsplit("_", 2)
        if len(parts) == 3:
            workload, sp_str, w_str = parts
        else:
            workload, sp_str, w_str = name, "0", "0"
            log.warning("无法解析 checkpoint 名: %s", name)

        try:
            sp = int(sp_str)
        except ValueError:
            sp = 0
        try:
            w = float(w_str)
        except ValueError:
            w = 0.0

        app = workload.split("_", 1)[0]
        return cls(raw=name, workload=workload, app=app, simpoint=sp, weight=w)


def _build_hierarchy(checkpoints: List[str]):
    ids = [CheckpointId.parse(cp) for cp in checkpoints]

    wk_map: Dict[str, List[str]] = {}
    for cid in ids:
        wk_map.setdefault(cid.workload, []).append(cid.raw)

    app_map: Dict[str, List[str]] = {}
    for wk in wk_map:
        app = wk.split("_", 1)[0]
        app_map.setdefault(app, []).append(wk)

    return ids, wk_map, app_map


# ═══════════════════════════════════════════════════════════════════════════
# §11  Main
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_DIR = (
    "/nfs/home/qiuzeyuan/repos/GEM5/test/optimize/output/"
    "RunOutput/InstLens/spec06/V3_NoPrefetch_CounterV2_1224"
)


def main():
    ap = argparse.ArgumentParser(description="InstLens — Oracle Profiler Visualizer")
    ap.add_argument("-i", "--input-dir",  default=DEFAULT_DIR, help="checkpoint 根目录")
    ap.add_argument("-o", "--output-dir", default="out_plots", help="输出目录")
    ap.add_argument("-j", "--job",    type=int, default=0, help="并行进程数，0=自动")
    ap.add_argument("-l", "--level",  type=int, choices=[1, 2, 3, 4, 5], default=2)
    ap.add_argument("-w", "--wrap",   type=int, default=80, help="标签换行宽度")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    _init_logger(args.verbose)
    os.makedirs(args.output_dir, exist_ok=True)

    all_cps = sorted(os.listdir(args.input_dir), key=str.lower)
    cp_ids, wk_map, app_map = _build_hierarchy(all_cps)
    template = args.input_dir + "/{}/m5out/oracle_profiler_thread_0.csv"

    log.info("发现 %d checkpoints, %d workloads, %d applications",
             len(all_cps), len(wk_map), len(app_map))
    if log.isEnabledFor(logging.DEBUG):
        for cid in cp_ids[:5]:
            log.debug("  样例: %s → app=%s, wk=%s, sp=%d, w=%.6f",
                      cid.raw, cid.app, cid.workload, cid.simpoint, cid.weight)

    ckpt_data = load_checkpoints(all_cps, template, n_jobs=args.job)

    wk_data = _group_and_aggregate(
        wk_map, ckpt_data,
        weight_fn=lambda k: ckpt_data[k].weight if k in ckpt_data else 0.0,
        desc="workloads",
        normalize=True,
    )

    app_data = _group_and_aggregate(
        app_map, wk_data,
        weight_fn=lambda k: wk_data[k].weight if k in wk_data else 0.0,
        desc="applications",
        normalize=False,
    )

    log.info("生成 HTML…")

    # ── Stage 4a: 导出 CSV ──
    csv_dir = os.path.join(args.output_dir, "csv")
    os.makedirs(csv_dir, exist_ok=True)
    for app_name, profile in sorted(app_data.items()):
        if not profile.pcs:
            continue
        export_csv(profile, os.path.join(csv_dir, f"{app_name}.csv"))
    log.info("  ✓ csv/ (%d files)", len(app_data))

    # ── Stage 4b: 渲染 HTML ──

    bar_profiles: Dict[str, Tuple[ProfileData, List[str]]] = {}
    pie_profiles: Dict[str, ProfileData] = {}

    for app_name, profile in sorted(app_data.items()):
        if not profile.pcs:
            log.warning("跳过空数据: %s", app_name)
            continue
        bar_profiles[app_name] = (profile, _build_labels(profile, wrap_width=args.wrap))
        pie_profiles[app_name] = profile

    if bar_profiles:
        render_all_stacked_bar(
            bar_profiles,
            os.path.join(args.output_dir, "all_stacked.html"),
            title="All Applications — Stacked Bar",
            top_options=[10, 20, 30, 50],
        )
        render_all_pie(
            pie_profiles,
            os.path.join(args.output_dir, "all_pie.html"),
            title="All Applications — Cycle Breakdown",
            default_level=args.level,
        )
        log.info("  ✓ all_stacked.html, all_pie.html")

    log.info("完成，输出目录: %s", args.output_dir)


if __name__ == "__main__":
    main()