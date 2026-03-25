"""
SPEC Score Calculator
=====================
Compute SPEC performance scores for completed gem5 simulation results.

Usage as library:
    from score import calculate_scores
    results = calculate_scores(cfg, ["260315_V3_NoPrefetcher"])
    for r in results:
        print(r.summary())

Usage as CLI (re-score without re-running):
    python score.py config.yaml
    python score.py config.yaml --arch 260315_V3_NoPrefetcher
"""

import logging
import os
import re
import subprocess
import sys
import argparse
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import Config

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


# ═══════════════════════════════════════════════════════════════
#  Result
# ═══════════════════════════════════════════════════════════════

@dataclass
class BenchmarkScore:
    name: str
    time: float
    ref_time: float
    score: float
    coverage: float


@dataclass
class ScoreResult:
    arch: str
    score_file: str

    int_per_ghz: Optional[float] = None
    int_at_3ghz: Optional[float] = None
    fp_per_ghz: Optional[float] = None
    fp_at_3ghz: Optional[float] = None
    overall_per_ghz: Optional[float] = None
    overall_at_3ghz: Optional[float] = None

    int_benchmarks: List[BenchmarkScore] = field(default_factory=list)
    fp_benchmarks: List[BenchmarkScore] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"  [{self.arch}]"]

        if self.int_per_ghz is not None:
            lines.append(f"    Int    : {self.int_per_ghz:8.3f} /GHz  {self.int_at_3ghz:8.3f} @3GHz")
        if self.fp_per_ghz is not None:
            lines.append(f"    FP     : {self.fp_per_ghz:8.3f} /GHz  {self.fp_at_3ghz:8.3f} @3GHz")
        if self.overall_per_ghz is not None:
            lines.append(f"    Overall: {self.overall_per_ghz:8.3f} /GHz  {self.overall_at_3ghz:8.3f} @3GHz")

        lines.append(f"    file: {self.score_file}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  Parser
# ═══════════════════════════════════════════════════════════════

_SCORE_RE = re.compile(r"Estimated (\w+) score (per GHz|@ [\d.]+GHz):\s+([\d.]+)")

# Matches lines like:  "libquantum  154.281   20720.0  44.767     0.243"
_BMK_ROW_RE = re.compile(
    r"^(\S+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*$"
)


def _parse_output(arch: str, score_file: str, stdout: str) -> ScoreResult:
    """Extract structured scores from the gem5-score-ci script output."""
    result = ScoreResult(arch=arch, score_file=score_file)

    for m in _SCORE_RE.finditer(stdout):
        category = m.group(1).lower()     # "int", "fp", "overall"
        kind = "per_ghz" if "per GHz" in m.group(2) else "at_3ghz"
        value = float(m.group(3))

        attr = f"{category}_{kind}"
        if hasattr(result, attr):
            setattr(result, attr, value)

    # parse per-benchmark tables
    # find the Int and FP sections
    section = None
    for line in stdout.splitlines():
        if "================ Int =================" in line:
            section = "int"
            continue
        elif "================ FP =================" in line:
            section = "fp"
            continue
        elif "================ Overall =================" in line:
            break

        if section is None:
            continue

        m = _BMK_ROW_RE.match(line.strip())
        if m and m.group(1) not in ("time", "mean"):
            bmk = BenchmarkScore(
                name=m.group(1),
                time=float(m.group(2)),
                ref_time=float(m.group(3)),
                score=float(m.group(4)),
                coverage=float(m.group(5)),
            )
            if section == "int":
                result.int_benchmarks.append(bmk)
            else:
                result.fp_benchmarks.append(bmk)

    return result


# ═══════════════════════════════════════════════════════════════
#  Core
# ═══════════════════════════════════════════════════════════════

def calculate_scores(cfg: Config, arch_names: List[str]) -> List[ScoreResult]:
    """
    Compute SPEC scores for each arch in *arch_names*.
    Returns list of ScoreResult with parsed scores.
    """
    data_proc = cfg.scoring.data_proc_home
    version_flag = "-17" if "2017" in cfg.preset_name else ""
    cluster_json = os.path.join(cfg.preset_path, "cluster-0-0.json")

    results = []
    for name in arch_names:
        arch_dir = os.path.join(cfg.run.output_dir, name)
        score_file = f"{arch_dir}.score.txt"

        if not os.path.isdir(arch_dir):
            log.warning("skip %s: directory not found (%s)", name, arch_dir)
            continue

        cmd = (
            f"export PYTHONPATH={data_proc}:$PYTHONPATH && "
            f"cd {data_proc} && "
            f"bash example-scripts/gem5-score-ci{version_flag}.sh"
            f" {arch_dir} {cluster_json}"
        )

        log.debug("Scoring %s ...", name)
        log.debug("cmd: %s", cmd)

        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)

        if proc.returncode != 0:
            log.error("scoring failed for %s (exit %d)", name, proc.returncode)
            if proc.stderr:
                log.error("stderr:\n%s", proc.stderr)
            continue

        # save full output
        with open(score_file, "w") as f:
            f.write(proc.stdout)

        # parse structured result
        result = _parse_output(name, score_file, proc.stdout)
        results.append(result)

        log.debug("full output saved to %s", score_file)

    return results


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="Compute SPEC scores for gem5 results")
    p.add_argument("config", help="YAML config path")
    p.add_argument("--arch", nargs="+",
                   help="Arch names to score (default: all in config)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config.load(args.config)
    arch_names = args.arch or [a.name for a in cfg.archs]

    log.info("Scoring %d arch(s): %s", len(arch_names), ", ".join(arch_names))

    results = calculate_scores(cfg, arch_names)

    if not results:
        log.error("No scores generated")
        sys.exit(1)

    # print clean summary
    print()
    print("=" * 60)
    print("  SPEC Score Summary")
    print("=" * 60)
    for r in results:
        print(r.summary())
    print()

    # comparison table if multiple archs
    if len(results) > 1:
        header = f"  {'arch':30s}  {'Int/GHz':>8s}  {'FP/GHz':>8s}  {'All/GHz':>8s}"
        print(header)
        print("  " + "─" * (len(header) - 2))
        for r in results:
            print(
                f"  {r.arch:30s}"
                f"  {r.int_per_ghz or 0:8.3f}"
                f"  {r.fp_per_ghz or 0:8.3f}"
                f"  {r.overall_per_ghz or 0:8.3f}"
            )
        print()


if __name__ == "__main__":
    main()