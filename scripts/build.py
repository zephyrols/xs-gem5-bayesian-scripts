"""
GEM5 Builder
============
Build gem5 (PGO or debug) and deploy the binary to bin_home.

Usage:
    python build.py config.yaml
    python build.py config.yaml --debug
    python build.py config.yaml --dry-run
    python build.py config.yaml -j 32
"""

import logging
import os
import shutil
import subprocess
import sys
import argparse
from pathlib import Path

from config import Config

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

def _make_env(cfg: Config) -> dict:
    """Build a subprocess-safe env dict with config-defined vars injected."""
    env = os.environ.copy()

    for item in cfg.env:
        env[item.name] = item.value
    return {k: str(v) if v is not None else "" for k, v in env.items()}


def _run(cmd: str, *, cwd: str, env: dict) -> subprocess.CompletedProcess:
    """Run a shell command. Logs output. Raises on failure."""
    log.debug("cwd: %s", cwd)
    log.debug("cmd: %s", cmd)
    result = subprocess.run(
        cmd, cwd=cwd, shell=True, env=env,
        capture_output=True, text=True,
    )
    if result.stdout:
        log.debug("stdout:\n%s", result.stdout)
    if result.stderr:
        log.warning("stderr:\n%s", result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed (exit {result.returncode}): {cmd}")
    return result


# ═══════════════════════════════════════════════════════════════
#  Build
# ═══════════════════════════════════════════════════════════════

def build_gem5(cfg: Config, *, debug: bool = False, jobs: int | None = None):
    """
    Build gem5 and copy the resulting binary to cfg.gem5.bin_home.

    Args:
        cfg:   Loaded Config object.
        debug: If True, build gem5.debug via scons; otherwise PGO via script.
        jobs:  Parallel build jobs (defaults to cpu_count).
    """
    gem5_home = Path(cfg.gem5.home)
    jobs = jobs or os.cpu_count() or 4
    env = _make_env(cfg)

    # ── choose build command & source binary ─────────────────
    if debug:
        cmd = f"scons build/RISCV/gem5.debug -j {jobs} --gold-linker"
        src = gem5_home / "build/RISCV/gem5.debug"
    else:
        pgo = gem5_home / "util/pgo/basic_pgo_new.sh"
        if not pgo.exists():
            raise FileNotFoundError(f"PGO script not found: {pgo}")
        cmd = str(pgo)
        src = gem5_home / "build/RISCV/gem5.fast"

    # ── build ────────────────────────────────────────────────
    mode = "debug" if debug else "PGO"
    log.debug("Building gem5 (%s, -j%d) ...", mode, jobs)
    _run(cmd, cwd=str(gem5_home), env=env)

    if not src.exists():
        raise FileNotFoundError(f"Build succeeded but binary missing: {src}")

    # ── deploy ───────────────────────────────────────────────
    dst = Path(cfg.gem5.bin_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    log.debug("Deploying %s → %s", src.name, dst)
    shutil.copy2(src, dst)
    dst.chmod(0o755)

    log.debug("Done: %s", dst)


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="Build GEM5 and deploy binary")
    p.add_argument("config", help="YAML config path")
    p.add_argument("--debug", action="store_true", help="Build debug instead of PGO")
    p.add_argument("--dry-run", action="store_true", help="Show config only")
    p.add_argument("-j", "--jobs", type=int, help="Build parallelism (default: nproc)")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config.load(args.config)

    if args.dry_run:
        log.info("DRY RUN\n%s", cfg.show())
        mode = "debug" if args.debug else "PGO"
        jobs = args.jobs or os.cpu_count() or 4
        log.info("Would build: %s, -j%d", mode, jobs)
        log.info("Target: %s", cfg.gem5.bin_path)
        return

    try:
        mode = "debug" if args.debug else "PGO"
        jobs = args.jobs or os.cpu_count() or 4
        log.info("Building gem5 (%s, -j%d) ...", mode, jobs)
        build_gem5(cfg, debug=args.debug, jobs=args.jobs)
        log.info("Done: %s", cfg.gem5.bin_path)
    except Exception as e:
        log.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()