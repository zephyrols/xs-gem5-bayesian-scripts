"""
GEM5 Config Loader
==================
Usage:
    cfg = Config.load("config.yaml")
    cfg.gem5.home
    cfg.gem5.bin_path          # derived: home/test/optimize/bin/{binary}
    cfg.gem5.data_proc_home    # derived: home/test/gem5_data_proc
    cfg.workloads[0].checkpoints
    cfg.archs[0].script_path   # derived: home/{script}
    cfg.cluster.servers
"""

import os
import re
import pathlib
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, computed_field


# ═══════════════════════════════════════════════════════════════
#  Models
# ═══════════════════════════════════════════════════════════════

class TypedPath(BaseModel):
    type: str
    path: str = ""


class Gem5(BaseModel):
    home: str
    bin_home: str
    binary: str
    restorer: TypedPath
    ref_so: TypedPath

    @computed_field
    @property
    def bin_path(self) -> str:
        return os.path.join(self.bin_home, self.binary)


class Scoring(BaseModel):
    data_proc_home: str


class Run(BaseModel):
    output_dir: str
    resume: bool = True
    checkpoint_weight: float = 1.0


class Cluster(BaseModel):
    max_procs_per_node: int = 64
    shell_init: List[str] = Field(default_factory=list)
    servers: List[str] = Field(default_factory=list)

    @computed_field
    @property
    def total_capacity(self) -> int:
        return self.max_procs_per_node * len(self.servers)


class Workload(BaseModel):
    name: str
    checkpoints: List[str]


class Arch(BaseModel):
    name: str
    script_path: str
    params: List[str]


class ParamDef(BaseModel):
    name: str
    type: str  # int | float | pow2 | choice | bool
    range: Optional[List[float]] = None
    values: Optional[List[Any]] = None


class Optimization(BaseModel):
    constants: List[str] = Field(default_factory=list)
    space: List[ParamDef] = Field(default_factory=list)

    def to_skopt(self):
        """list[skopt.space.Dimension]. Lazy-imports scikit-optimize."""
        from skopt.space import Categorical, Integer, Real
        builders = {
            "int":    lambda p: Integer(int(p.range[0]), int(p.range[1]), name=p.name),
            "float":  lambda p: Real(p.range[0], p.range[1], name=p.name),
            "pow2":   lambda p: Categorical([2**i for i in range(int(p.range[0]), int(p.range[1]) + 1)], name=p.name),
            "choice": lambda p: Categorical(p.values, name=p.name),
            "bool":   lambda p: Categorical([True, False], name=p.name),
        }
        return [builders[p.type](p) for p in self.space]


# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

_WEIGHT_RE = re.compile(r"(\d+)_([0-9]*\.?[0-9]+)")


def _discover_checkpoints(root: str, name: str, weight: float) -> List[str]:
    """Find checkpoint files sorted by simpoint weight, accumulated up to *weight*."""
    dirs = [d for d in pathlib.Path(root).glob(name) if d.is_dir()]
    files = [
        p for d in dirs
        for ext in ("zstd", "gz")
        for p in d.glob(f"**/*.{ext}") if p.is_file()
    ]

    weighted = []
    for f in files:
        m = _WEIGHT_RE.findall(f.name)
        if m:
            weighted.append((float(m[0][1]), str(f)))
    weighted.sort(reverse=True)

    selected, total = [], 0.0
    for w, path in weighted:
        if total >= weight:
            break
        selected.append(path)
        total += w
    return selected


# ═══════════════════════════════════════════════════════════════
#  Root Config
# ═══════════════════════════════════════════════════════════════

class Config(BaseModel):
    gem5: Gem5
    run: Run
    scoring: Scoring
    cluster: Cluster
    workloads: List[Workload]
    archs: List[Arch]
    preset_name: str = ""          # e.g. "spec2006", "spec2017"
    preset_path: str = ""          # root path of the active preset's checkpoints
    optimization: Optional[Optimization] = None

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path) as f:
            raw = yaml.safe_load(f)

        # ── gem5 ─────────────────────────────────────────────
        gem5 = Gem5(**raw["gem5"])

        # ── run ──────────────────────────────────────────────
        run_raw = raw["run"]
        run = Run(
            output_dir=os.path.abspath(run_raw["output_dir"]),
            resume=run_raw.get("resume", True),
            checkpoint_weight=run_raw.get("checkpoint_weight", 1.0),
        )

        # ── cluster ──────────────────────────────────────────
        cluster = Cluster(**raw.get("cluster", {}))

        # ── scoring ──────────────────────────────────────────
        scoring = Scoring(**raw["scoring"])

        # ── workloads ────────────────────────────────────────
        wl_cfg = raw["workloads"]
        preset_key = wl_cfg["preset"]
        presets = raw.get("presets", {})

        if preset_key not in presets:
            available = ", ".join(presets) or "(none)"
            raise ValueError(f"Preset '{preset_key}' not found. Available: {available}")

        preset = presets[preset_key]
        names: List[str] = list(preset["list"])

        if only := wl_cfg.get("only"):
            keep = set(only)
            names = [n for n in names if n in keep]
        if exclude := wl_cfg.get("exclude"):
            drop = set(exclude)
            names = [n for n in names if n not in drop]

        workloads = [
            Workload(
                name=n,
                checkpoints=_discover_checkpoints(
                    preset["path"], n, run.checkpoint_weight,
                ),
            )
            for n in names
        ]

        # ── archs ────────────────────────────────────────────
        archs_raw = raw.get("archs", {})
        defaults = archs_raw.get("defaults", {})
        default_script = defaults.get("script", "configs/example/xiangshan.py")
        default_params = defaults.get("params", [])

        archs = [
            Arch(
                name=a["name"],
                script_path=os.path.join(gem5.home, a.get("script", default_script)),
                params=a.get("params", list(default_params)),
            )
            for a in archs_raw.get("configs", [])
        ]

        # ── optimization ─────────────────────────────────────
        opt = Optimization(**raw["optimization"]) if "optimization" in raw else None

        return cls(
            gem5=gem5, run=run, scoring=scoring, cluster=cluster,
            workloads=workloads, archs=archs,
            preset_name=preset_key, preset_path=preset["path"],
            optimization=opt,
        )

    # ── display ──────────────────────────────────────────────

    def show(self, verbose: bool = False) -> str:
        lines: List[str] = []

        def h(title: str):
            lines.append(f"\n{'─' * 60}")
            lines.append(f"  {title}")
            lines.append(f"{'─' * 60}")

        h("GEM5")
        lines.append(f"  home           : {self.gem5.home}")
        lines.append(f"  bin_home       : {self.gem5.bin_home}")
        lines.append(f"  binary         : {self.gem5.bin_path}")
        lines.append(f"  restorer       : {self.gem5.restorer.type}  {self.gem5.restorer.path or '(embedded)'}")
        lines.append(f"  ref_so         : {self.gem5.ref_so.type}  {self.gem5.ref_so.path}")

        h("Run")
        lines.append(f"  output_dir        : {self.run.output_dir}")
        lines.append(f"  resume            : {self.run.resume}")
        lines.append(f"  checkpoint_weight : {self.run.checkpoint_weight}")

        h("Scoring")
        lines.append(f"  data_proc_home : {self.scoring.data_proc_home}")

        h(f"Cluster ({len(self.cluster.servers)} nodes, capacity {self.cluster.total_capacity})")
        lines.append(f"  max_procs_per_node : {self.cluster.max_procs_per_node}")
        if self.cluster.shell_init:
            lines.append(f"  shell_init:")
            for cmd in self.cluster.shell_init:
                lines.append(f"    $ {cmd}")
        for i, s in enumerate(self.cluster.servers, 1):
            lines.append(f"  {i:2d}. {s}")

        h(f"Workloads ({len(self.workloads)}, preset: {self.preset_name})")
        lines.append(f"  preset_path : {self.preset_path}")
        for w in self.workloads:
            lines.append(f"  {w.name:30s}  {len(w.checkpoints)} cpts")
            if verbose:
                for c in w.checkpoints:
                    lines.append(f"      {c}")

        h(f"Archs ({len(self.archs)})")
        for a in self.archs:
            lines.append(f"  [{a.name}]")
            lines.append(f"    {a.script_path}")
            lines.append(f"    {' '.join(a.params)}")

        if self.optimization:
            h("Optimization")
            for c in self.optimization.constants:
                lines.append(f"  const: {c}")
            for p in self.optimization.space:
                lines.append(f"  {p.name:20s}  {p.type}  {p.range or p.values or ''}")

        return "\n".join(lines)

    def __repr__(self):
        return (
            f"Config(archs={len(self.archs)}, "
            f"workloads={len(self.workloads)}, "
            f"cluster={len(self.cluster.servers)} nodes)"
        )


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import logging
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    p = argparse.ArgumentParser(description="GEM5 config viewer")
    p.add_argument("config", help="YAML config path")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.info("Loading config: %s", args.config)
    cfg = Config.load(args.config)
    logging.info("\n%s", cfg.show(verbose=args.verbose))