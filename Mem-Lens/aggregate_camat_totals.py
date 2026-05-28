#!/usr/bin/env python3
"""聚合 checkpoint 目录下 L1D/L2/L3 的 CAMAT 指标（新格式）。

默认递归扫描输入目录中的 m5out/stats.txt，提取如下新格式字段：
- system.xxx.camatTotalAccesses
- system.xxx.camatHitLatency
- system.xxx.camatHitClock
- system.xxx.camatPureMisses
- system.xxx.camatPureMissPenalty
- system.xxx.camatPureMissClock

并优先由上述原始计数器重算：
- camatAvgHitLatency
- camatConcurrencyHit
- camatPureMissRate
- camatAvgPureMissPenalty
- camatConcurrencyPureMiss
- camatConcurrentAvgMemoryAccessTime

输出两个 CSV：
1) checkpoint 级明细
2) level+metric 聚合统计（count/mean/min/max）
3) workload 级加权聚合（按 checkpoint 名中的权重）
"""

from __future__ import annotations

import argparse
import csv
import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


LEVEL_PREFIX = {
    "L1D": "system.cpu.dcache",
    "L2": "system.l2_caches",
    "L3": "system.l3",
}

METRICS = [
    "camatAvgHitLatency",
    "camatConcurrencyHit",
    "camatPureMissRate",
    "camatAvgPureMissPenalty",
    "camatConcurrencyPureMiss",
    "camatConcurrentAvgMemoryAccessTime",
    "demandMissRate",
    "demandAvgMissLatency",
    "wayPreHitTimes",
    "wayPreTimes",
]

RAW_METRICS = [
    "camatTotalAccesses",
    "camatHitLatency",
    "camatHitClock",
    "camatPureMisses",
    "camatPureMissPenalty",
    "camatPureMissClock",
]

TOTAL_SUFFIX_METRICS = {
    "demandMissRate",
    "demandAvgMissLatency",
}

CONCURRENT_METRIC = "camatConcurrentAvgMemoryAccessTime"
CONCURRENT_COMPONENT_METRICS = [
    "camatAvgHitLatency",
    "camatConcurrencyHit",
    "camatPureMissRate",
    "camatAvgPureMissPenalty",
    "camatConcurrencyPureMiss",
]

OUTPUT_DECIMALS = 8
SUMMARY_DECIMALS = 8


def is_finite_number(v: Optional[float]) -> bool:
    return v is not None and not math.isnan(v) and not math.isinf(v)


def metric_keys() -> List[Tuple[str, str]]:
    return [(level, metric) for level in LEVEL_PREFIX for metric in METRICS]


def metric_column_names() -> List[str]:
    return [f"{level}.{metric}" for level in LEVEL_PREFIX for metric in METRICS]


def find_stats_files(root: Path) -> List[Path]:
    return sorted(root.rglob("m5out/stats.txt"))


def checkpoint_name_from_stats(stats_path: Path) -> str:
    # .../<checkpoint>/m5out/stats.txt
    if stats_path.parent.name == "m5out":
        return stats_path.parent.parent.name
    return stats_path.parent.name


def workload_and_weight_from_checkpoint(checkpoint: str) -> Tuple[str, Optional[float]]:
    # 约定1: app_input_insts_weight -> workload = app_input
    # 约定2: app_insts_weight -> workload = app
    parts = checkpoint.split("_")
    if len(parts) < 3:
        return checkpoint, None

    weight = parse_float(parts[-1])
    try:
        # insts 字段应为整数；若不符合则认为格式异常
        int(parts[-2])
    except ValueError:
        return checkpoint, None

    if len(parts) == 3:
        workload = parts[0]
    else:
        workload = "_".join(parts[:-2])

    if not workload or weight is None:
        workload = checkpoint
    return workload, weight


def parse_float(token: str) -> Optional[float]:
    txt = token.strip().lower()
    if txt in {"nan", "+nan", "-nan"}:
        return math.nan
    if txt in {"inf", "+inf", "infinity", "+infinity"}:
        return math.inf
    if txt in {"-inf", "-infinity"}:
        return -math.inf
    try:
        return float(token)
    except ValueError:
        return None


def extract_last_values(stats_path: Path) -> Dict[Tuple[str, str], Optional[float]]:
    # key: (level, metric) -> value; 遇到重复项时保留最后一次。
    result: Dict[Tuple[str, str], Optional[float]] = {}

    patterns = {}
    for level, prefix in LEVEL_PREFIX.items():
        for metric in RAW_METRICS + METRICS:
            key = (level, metric)
            suffix = r"::total" if metric in TOTAL_SUFFIX_METRICS else ""
            patterns[key] = re.compile(
                rf"^{re.escape(prefix)}\.{re.escape(metric)}{suffix}\s+([^\s#]+)"
            )

    with stats_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            for key, pat in patterns.items():
                m = pat.match(text)
                if m:
                    result[key] = parse_float(m.group(1))

    return result


def derive_metrics_from_raw(
    values: Dict[Tuple[str, str], Optional[float]], level: str
) -> Dict[str, Optional[float]]:
    total = values.get((level, "camatTotalAccesses"))
    hit_lat = values.get((level, "camatHitLatency"))
    hit_clk = values.get((level, "camatHitClock"))
    pm_cnt = values.get((level, "camatPureMisses"))
    pm_pen = values.get((level, "camatPureMissPenalty"))
    pm_clk = values.get((level, "camatPureMissClock"))

    raw_vals = [total, hit_lat, hit_clk, pm_cnt, pm_pen, pm_clk]
    if any(not is_finite_number(v) for v in raw_vals):
        return {}

    if total == 0 or hit_clk == 0 or pm_cnt == 0 or pm_clk == 0:
        return {}

    avg_hit = hit_lat / total
    conc_hit = hit_lat / hit_clk
    pm_rate = pm_cnt / total
    avg_pm_pen = pm_pen / pm_cnt
    conc_pm = pm_pen / pm_clk

    if conc_hit == 0 or conc_pm == 0:
        return {}

    concurrent = avg_hit / conc_hit + pm_rate * avg_pm_pen / conc_pm

    return {
        "camatAvgHitLatency": avg_hit,
        "camatConcurrencyHit": conc_hit,
        "camatPureMissRate": pm_rate,
        "camatAvgPureMissPenalty": avg_pm_pen,
        "camatConcurrencyPureMiss": conc_pm,
        "camatConcurrentAvgMemoryAccessTime": concurrent,
    }


def derive_concurrent_camat_from_totals(
    totals: Dict[Tuple[str, str], Optional[float]], level: str
) -> Optional[float]:
    hit = totals.get((level, "camatAvgHitLatency"))
    conc_hit = totals.get((level, "camatConcurrencyHit"))
    pure_miss = totals.get((level, "camatPureMissRate"))
    penalty = totals.get((level, "camatAvgPureMissPenalty"))
    conc_pure_miss = totals.get((level, "camatConcurrencyPureMiss"))

    vals = [hit, conc_hit, pure_miss, penalty, conc_pure_miss]
    if any(not is_finite_number(v) for v in vals):
        return None
    if conc_hit == 0 or conc_pure_miss == 0:
        return None

    return hit / conc_hit + pure_miss * penalty / conc_pure_miss


def fmt(v: Optional[float]) -> str:
    if v is None:
        return ""
    if math.isnan(v):
        return "nan"
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    try:
        quant = Decimal("1").scaleb(-OUTPUT_DECIMALS)
        d = Decimal(str(v)).quantize(quant, rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ValueError):
        return ""
    return f"{d:.{OUTPUT_DECIMALS}f}"


def valid_numeric(values: Iterable[Optional[float]]) -> List[float]:
    return [v for v in values if is_finite_number(v)]


def compute_concurrent_from_components(components: Dict[str, float]) -> Optional[float]:
    ch = components["camatConcurrencyHit"]
    cm = components["camatConcurrencyPureMiss"]
    if ch == 0 or cm == 0:
        return None
    return (
        components["camatAvgHitLatency"] / ch
        + components["camatPureMissRate"] * components["camatAvgPureMissPenalty"] / cm
    )


def get_weighted_metric(
    acc: Dict[Tuple[str, str], List[float]],
    level: str,
    metric: str,
) -> Optional[float]:
    num, den = acc[(level, metric)]
    if den <= 0:
        return None
    return num / den


def ensure_workload_bucket(
    workload: str,
    all_metric_keys: List[Tuple[str, str]],
    workload_acc: Dict[str, Dict[Tuple[str, str], List[float]]],
    workload_weighted_ckpt_count: Dict[str, int],
    workload_total_weight: Dict[str, float],
) -> None:
    if workload in workload_acc:
        return
    workload_acc[workload] = {k: [0.0, 0.0] for k in all_metric_keys}
    workload_weighted_ckpt_count[workload] = 0
    workload_total_weight[workload] = 0.0


def append_checkpoint_metrics(
    values: Dict[Tuple[str, str], Optional[float]],
    workload: str,
    weight: Optional[float],
    row: Dict[str, str],
    matrix: Dict[Tuple[str, str], List[Optional[float]]],
    workload_acc: Dict[str, Dict[Tuple[str, str], List[float]]],
) -> None:
    weight_valid = is_finite_number(weight) and weight > 0
    for level in LEVEL_PREFIX:
        # 新格式下优先使用 raw counter 重算全部派生指标。
        derived = derive_metrics_from_raw(values, level)
        for metric, val in derived.items():
            values[(level, metric)] = val

        # 若 raw 缺失，则退化为使用现有分量重算 concurrent 指标。
        if (level, CONCURRENT_METRIC) not in values:
            derived_concurrent = derive_concurrent_camat_from_totals(values, level)
            if derived_concurrent is not None:
                values[(level, CONCURRENT_METRIC)] = derived_concurrent

        for metric in METRICS:
            key = (level, metric)
            val = values.get(key)
            row[f"{level}.{metric}"] = fmt(val)
            matrix[key].append(val)

            if weight_valid and is_finite_number(val):
                workload_acc[workload][key][0] += val * weight
                workload_acc[workload][key][1] += weight


def build_summary_rows(
    matrix: Dict[Tuple[str, str], List[Optional[float]]]
) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for level in LEVEL_PREFIX:
        for metric in METRICS:
            data = valid_numeric(matrix[(level, metric)])
            if not data:
                rows.append(
                    {
                        "level": level,
                        "metric": metric,
                        "count": "0",
                        "mean": "",
                        "min": "",
                        "max": "",
                    }
                )
                continue

            rows.append(
                {
                    "level": level,
                    "metric": metric,
                    "count": str(len(data)),
                    "mean": f"{sum(data) / len(data):.{SUMMARY_DECIMALS}f}",
                    "min": f"{min(data):.{SUMMARY_DECIMALS}f}",
                    "max": f"{max(data):.{SUMMARY_DECIMALS}f}",
                }
            )
    return rows


def build_workload_rows(
    workload_acc: Dict[str, Dict[Tuple[str, str], List[float]]],
    workload_ckpt_count: Dict[str, int],
    workload_weighted_ckpt_count: Dict[str, int],
    workload_total_weight: Dict[str, float],
) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for workload in sorted(workload_acc.keys()):
        if workload_weighted_ckpt_count.get(workload, 0) == 0:
            continue

        row: Dict[str, str] = {
            "workload": workload,
            "checkpoint_count": str(workload_ckpt_count.get(workload, 0)),
            "weighted_checkpoint_count": str(workload_weighted_ckpt_count.get(workload, 0)),
            "total_weight": fmt(workload_total_weight.get(workload, 0.0)),
        }
        for level in LEVEL_PREFIX:
            # 先输出每个指标的 workload 加权均值。
            for metric in METRICS:
                mean_val = get_weighted_metric(workload_acc[workload], level, metric)
                row[f"{level}.{metric}"] = fmt(mean_val) if mean_val is not None else ""

            # workload 的 concurrent AMAT 使用 workload 级加权分量重新计算，
            # 而不是对 checkpoint 级 concurrent 直接加权。
            components: Dict[str, float] = {}
            for metric in CONCURRENT_COMPONENT_METRICS:
                value = get_weighted_metric(workload_acc[workload], level, metric)
                if value is None:
                    components = {}
                    break
                components[metric] = value

            if components:
                concurrent = compute_concurrent_from_components(components)
                row[f"{level}.{CONCURRENT_METRIC}"] = fmt(concurrent) if concurrent is not None else ""
            else:
                row[f"{level}.{CONCURRENT_METRIC}"] = ""
        rows.append(row)
    return rows


def write_checkpoint_csv(rows: List[Dict[str, str]], out_file: Path) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["checkpoint", *metric_column_names()]

    with out_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_summary_csv(rows: List[Dict[str, str]], out_file: Path) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["level", "metric", "count", "mean", "min", "max"]
    with out_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_workload_csv(rows: List[Dict[str, str]], out_file: Path) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "workload",
        "checkpoint_count",
        "weighted_checkpoint_count",
        "total_weight",
        *metric_column_names(),
    ]

    with out_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="聚合 checkpoint CAMAT total 指标")
    parser.add_argument("input_dir", help="根目录，脚本将递归扫描 m5out/stats.txt")
    parser.add_argument(
        "--out-dir",
        default=".",
        help="输出目录（默认当前目录）",
    )
    parser.add_argument(
        "--prefix",
        default="camat_totals",
        help="输出文件名前缀（默认 camat_totals）",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()

    if not input_dir.exists() or not input_dir.is_dir():
        print(f"错误: 输入目录不存在或不是目录: {input_dir}")
        return 1

    stats_files = find_stats_files(input_dir)
    if not stats_files:
        print(f"未找到 stats.txt: {input_dir}")
        return 1

    all_metric_keys = metric_keys()
    checkpoint_rows: List[Dict[str, str]] = []
    matrix: Dict[Tuple[str, str], List[Optional[float]]] = {k: [] for k in all_metric_keys}
    workload_acc: Dict[str, Dict[Tuple[str, str], List[float]]] = {}
    workload_ckpt_count: Dict[str, int] = {}
    workload_weighted_ckpt_count: Dict[str, int] = {}
    workload_total_weight: Dict[str, float] = {}

    for stats_path in stats_files:
        checkpoint = checkpoint_name_from_stats(stats_path)
        values = extract_last_values(stats_path)
        workload, weight = workload_and_weight_from_checkpoint(checkpoint)

        workload_ckpt_count[workload] = workload_ckpt_count.get(workload, 0) + 1
        ensure_workload_bucket(
            workload,
            all_metric_keys,
            workload_acc,
            workload_weighted_ckpt_count,
            workload_total_weight,
        )

        # 仅允许正权重参与 workload 加权统计。
        weight_valid = is_finite_number(weight) and weight > 0
        if weight_valid:
            workload_weighted_ckpt_count[workload] += 1
            workload_total_weight[workload] += weight

        row: Dict[str, str] = {"checkpoint": checkpoint}
        append_checkpoint_metrics(values, workload, weight, row, matrix, workload_acc)
        checkpoint_rows.append(row)

    checkpoint_csv = out_dir / f"{args.prefix}_by_checkpoint.csv"
    write_checkpoint_csv(checkpoint_rows, checkpoint_csv)

    summary_rows = build_summary_rows(matrix)

    summary_csv = out_dir / f"{args.prefix}_summary.csv"
    write_summary_csv(summary_rows, summary_csv)

    workload_rows = build_workload_rows(
        workload_acc,
        workload_ckpt_count,
        workload_weighted_ckpt_count,
        workload_total_weight,
    )

    workload_csv = out_dir / f"{args.prefix}_by_workload.csv"
    write_workload_csv(workload_rows, workload_csv)

    print(f"已解析 stats 文件数: {len(stats_files)}")
    print(f"checkpoint 明细: {checkpoint_csv}")
    print(f"聚合汇总: {summary_csv}")
    print(f"workload 加权汇总: {workload_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
