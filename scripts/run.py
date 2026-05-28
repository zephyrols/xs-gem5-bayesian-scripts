"""
GEM5 Simulation Runner
======================
Issue gem5 simulations across a server cluster, monitor progress,
and compute performance scores.

Usage:
    python run.py config.yaml
    python run.py config.yaml -v
"""

import logging
import os
import re
import sys
import time
import argparse
from datetime import timedelta
from typing import List

from tqdm import tqdm

import remote
import checkrun
from config import Config, Arch, Workload
from score import calculate_scores

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  Logging handler that plays nicely with tqdm
# ═══════════════════════════════════════════════════════════════

class TqdmHandler(logging.Handler):
    """Route all log output through tqdm.write so progress bars stay intact."""

    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg)
        except Exception:
            self.handleError(record)


# ═══════════════════════════════════════════════════════════════
#  Issue
# ═══════════════════════════════════════════════════════════════

def run_cmd(cfg: Config, workload: Workload, arch: Arch):
    """
    Distribute gem5 checkpoint simulations for one (workload, arch) pair
    across the server cluster.
    """
    gem5 = cfg.gem5
    servers = cfg.cluster.servers
    max_procs = cfg.cluster.max_procs_per_node

    env_vars = {item.name: item.value for item in cfg.env}

    for cpt in tqdm(workload.checkpoints,
                    desc=f"Issuing {workload.name}", leave=False,
                    unit="cpt", dynamic_ncols=True):

        # ── parse checkpoint id ──────────────────────────────
        matches = re.findall(r'(\d+)_([0-9]*\.?[0-9]+)', os.path.basename(cpt))
        inst_num, weight = matches[0]

        # ── output dir ───────────────────────────────────────
        cpt_output_dir = os.path.join(
            cfg.run.output_dir, arch.name,
            f"{workload.name}_{inst_num}_{weight}",
        )
        os.makedirs(cpt_output_dir, exist_ok=True)

        # ── skip completed ───────────────────────────────────
        if cfg.run.resume and checkrun.check_run(cpt_output_dir).complete == 1:
            continue

        # ── build remote command ─────────────────────────────
        env_setup = list(cfg.cluster.shell_init)
        for name, value in env_vars.items():
            env_setup.append(f"export {name}={value}")

        dir_setup = [
            f"mkdir -p {cpt_output_dir}",
            f"cd {cpt_output_dir}",
        ]

        gem5_cmd = " ".join([
            gem5.bin_path,
            "--redirect-stdout",
            "--redirect-stderr",
            arch.script_path,
            f"--generic-rv-cpt={cpt}",
            *arch.params,
            "&",
        ])

        cmd = "; ".join(env_setup + dir_setup + [gem5_cmd])

        # ── distribute until placed ──────────────────────────
        placed = False
        while not placed:
            for server in servers:
                time.sleep(2)
                placed = remote.check_load_and_run(
                    server, cmd,
                    os.path.basename(gem5.bin_path),
                    max_procs,
                )
                if placed:
                    log.info("→ %s  %s", server, cpt_output_dir)
                    break


def issue_archs(cfg: Config) -> List[str]:
    """
    Issue all (arch × workload) combinations.
    Returns list of successfully issued arch names.
    """
    issued = []

    for arch in tqdm(cfg.archs, desc="Architectures", unit="arch", dynamic_ncols=True):
        try:
            t0 = time.time()
            for workload in tqdm(cfg.workloads,
                                 desc=f"  {arch.name}", leave=False,
                                 unit="wl", dynamic_ncols=True):
                run_cmd(cfg, workload, arch)

            elapsed = str(timedelta(seconds=int(time.time() - t0)))
            log.info("✓ %s issued in %s", arch.name, elapsed)
            issued.append(arch.name)

        except Exception as e:
            log.error("Failed issuing %s: %s", arch.name, e)

    return issued


# ═══════════════════════════════════════════════════════════════
#  Monitor
# ═══════════════════════════════════════════════════════════════

def monitor_progress(cfg: Config, arch_names: List[str],
                     interval: int = 2) -> List[str]:
    """
    Poll checkpoint completion for each arch until all are done.
    Returns list of finished arch names.
    """
    trackers = {}
    finished = set()

    for name in arch_names:
        r = checkrun.check_run(os.path.join(cfg.run.output_dir, name))
        trackers[name] = {
            "bar": tqdm(total=r.total, initial=r.complete + r.error,
                        desc=f"Progress {name}", unit="cpt",
                        dynamic_ncols=True),
            "last": r,
        }

    while finished != set(arch_names):
        for name in set(arch_names) - finished:
            r = checkrun.check_run(os.path.join(cfg.run.output_dir, name))

            tr = trackers[name]
            tr["bar"].n = r.complete + r.error
            tr["bar"].refresh()
            tr["last"] = r

            if r.finished:
                finished.add(name)
                log.info("✓ %s | ok=%d/%d err=%d/%d",
                         name, r.complete, r.total, r.error, r.total)

        if finished != set(arch_names):
            time.sleep(interval)

    for tr in trackers.values():
        tr["bar"].close()

    return list(finished)


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="GEM5 simulation runner")
    p.add_argument("config", help="YAML config path")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Enable debug logging")
    args = p.parse_args()

    # Use TqdmHandler so log output doesn't corrupt progress bars
    handler = TqdmHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    logging.root.addHandler(handler)
    logging.root.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    cfg = Config.load(args.config)
    log.debug("Loaded config: %s", cfg)

    # 1) issue
    issued = issue_archs(cfg)
    if not issued:
        log.error("No architectures were issued successfully")
        sys.exit(1)

    # 2) monitor
    finished = monitor_progress(cfg, issued)

    # 3) score
    results = calculate_scores(cfg, finished)
    for r in results:
        log.info("\n%s", r.summary())


if __name__ == "__main__":
    main()