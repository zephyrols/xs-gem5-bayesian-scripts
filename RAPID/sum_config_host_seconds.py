#!/usr/bin/env python3
"""统计 V3_PRT_1024 下各配置与各应用的运行时间。

规则：
- 每个切片目录的 m5out/stats.txt 中会出现两条 hostSeconds，二者都计入。
- 一个配置下所有切片的 hostSeconds 之和，作为该配置总运行时间。
- 应用级支持计算：
    - 3个独立候选预取器总时间（stream/stride/cplx）
    - 全部组合总时间（3独立 + 两两组合 + 三者组合）
    - 加速比 = 全部组合总时间 / 独立候选总时间
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


HOST_SECONDS_RE = re.compile(r"^hostSeconds\s+([0-9]+(?:\.[0-9]+)?)\b")


def find_stats_files(root: Path) -> List[Path]:
    return sorted(root.rglob("m5out/stats.txt"))


def extract_host_seconds(stats_file: Path) -> float:
    total = 0.0
    with stats_file.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = HOST_SECONDS_RE.match(line.strip())
            if m:
                total += float(m.group(1))
    return total


def group_by_config(root: Path, stats_files: Iterable[Path]) -> Dict[str, float]:
    config_totals: Dict[str, float] = defaultdict(float)

    for stats in stats_files:
        rel = stats.relative_to(root)
        parts = rel.parts
        if len(parts) < 3:
            # 至少应为 <config>/<slice>/m5out/stats.txt
            continue
        config_name = parts[0]
        config_totals[config_name] += extract_host_seconds(stats)

    return dict(config_totals)


def app_name_from_slice(slice_name: str) -> str:
    # 格式通常为 app_input_insts_weight 或 app_insts_weight
    return slice_name.split("_")[0] if "_" in slice_name else slice_name





def classify_config(config_name: str) -> Optional[str]:
    if config_name is None:
        return None
    parts = config_name.split("_")
    if len(set(parts)) != len(parts):
        return None
    
    if len(parts) == 1:
        if config_name == "none":
            return "none"
        return "single"
    return "combo"


def group_by_config_and_app(root: Path, stats_files: Iterable[Path]) -> Dict[str, Dict[str, float]]:
    # config -> app -> hostSeconds_total
    out: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for stats in stats_files:
        rel = stats.relative_to(root)
        parts = rel.parts
        if len(parts) < 3:
            continue
        config_name = parts[0]
        slice_name = parts[1]
        app = app_name_from_slice(slice_name)
        out[config_name][app] += extract_host_seconds(stats)
    return out


def compute_app_speedup(
    config_app_totals: Dict[str, Dict[str, float]]
) -> List[Dict[str, str]]:
    single_cfgs: Set[str] = set()
    all_combo_cfgs: Set[str] = set()
    for cfg in config_app_totals:
        kind = classify_config(cfg)
        print(cfg, kind)
        if kind is None:
            continue
        if kind != "none":
            all_combo_cfgs.add(cfg)
        if kind == "single" or kind == "none":
            single_cfgs.add(cfg)

    all_apps: Set[str] = set()
    for app_map in config_app_totals.values():
        all_apps.update(app_map.keys())

    rows: List[Dict[str, str]] = []
    for app in sorted(all_apps):
        single_total = sum(config_app_totals[cfg].get(app, 0.0) for cfg in single_cfgs)
        all_combo_total = sum(config_app_totals[cfg].get(app, 0.0) for cfg in all_combo_cfgs)

        if single_total <= 0:
            speedup = ""
            saving_ratio = ""
        else:
            speedup_val = all_combo_total / single_total
            speedup = f"{speedup_val:.4f}"
            saving_ratio = f"{(1.0 - single_total / all_combo_total):.4f}" if all_combo_total > 0 else ""

        rows.append(
            {
                "app": app,
                "single_total_hostSeconds": f"{single_total:.2f}",
                "all_combo_total_hostSeconds": f"{all_combo_total:.2f}",
                "speedup_all_over_single": speedup,
                "time_saving_ratio": saving_ratio,
            }
        )
    return rows


def write_csv(config_totals: Dict[str, float], out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["config", "hostSeconds_total"])
        for cfg in sorted(config_totals):
            writer.writerow([cfg, f"{config_totals[cfg]:.2f}"])


def write_app_speedup_csv(rows: List[Dict[str, str]], out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "app",
            "single_total_hostSeconds",
            "all_combo_total_hostSeconds",
            "speedup_all_over_single",
            "time_saving_ratio",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总各配置总运行时间（hostSeconds）")
    parser.add_argument(
        "input_dir",
        nargs="?",
        default="/nfs/home/qiuzeyuan/repos/GEM5/test/optimize/output/RAPID/mySpec06-full/L1",
        help="待统计目录，默认 V3_PRT_1024",
    )
    parser.add_argument(
        "--csv",
        default="/nfs/home/qiuzeyuan/repos/GEM5/test/optimize/RAPID/runtime_summary.csv",
        help="CSV 输出路径",
    )
    parser.add_argument(
        "--app-csv",
        default="/nfs/home/qiuzeyuan/repos/GEM5/test/optimize/RAPID/app_speedup.csv",
        help="应用级 speedup CSV 输出路径",
    )
    args = parser.parse_args()

    root = Path(args.input_dir).expanduser().resolve()
    out_csv = Path(args.csv).expanduser().resolve()
    out_app_csv = Path(args.app_csv).expanduser().resolve()

    if not root.exists() or not root.is_dir():
        print(f"错误: 输入目录不存在或不是目录: {root}")
        return 1

    stats_files = find_stats_files(root)
    if not stats_files:
        print(f"错误: 未找到 m5out/stats.txt: {root}")
        return 1

    totals = group_by_config(root, stats_files)
    if not totals:
        print("错误: 未统计到有效配置")
        return 1

    write_csv(totals, out_csv)

    config_app_totals = group_by_config_and_app(root, stats_files)
    app_rows = compute_app_speedup(config_app_totals)
    write_app_speedup_csv(app_rows, out_app_csv)

    print(f"扫描 stats.txt 数量: {len(stats_files)}")
    print("各配置总运行时间(hostSeconds):")
    grand_total = 0.0
    for cfg in sorted(totals):
        v = totals[cfg]
        grand_total += v
        print(f"  {cfg}: {v:.2f}")
    print(f"全部配置总和: {grand_total:.2f}")
    print(f"CSV 已写入: {out_csv}")
    print(f"应用级 speedup CSV 已写入: {out_app_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
