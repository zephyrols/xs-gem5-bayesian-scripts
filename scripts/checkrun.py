"""
Checkpoint Run Status Checker
==============================
Scan simulation output directories and classify each checkpoint as
complete / error / running.

Usage:
    python checkrun.py ./output/RunOutput/InstLens/spec06/some_arch
    python checkrun.py ./output/RunOutput/InstLens/spec06/some_arch -v
"""

import logging
import os
import re
import argparse
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


# ═══════════════════════════════════════════════════════════════
#  Patterns
# ═══════════════════════════════════════════════════════════════

_SUCCESS_PATTERNS = [
    re.compile(r"because a thread reached the max instruction count"),
    re.compile(r"because m5_exit instruction encountered when simulating XS"),
]

_ERROR_PATTERNS = [
    re.compile(r"Program aborted at tick"),
    re.compile(r"Failed to execute default signal handler!"),
    re.compile(r"gem5 has encountered a segmentation fault!"),
    re.compile(r"error: ambiguous option:"),
    re.compile(r"AttributeError:"),
]


class Status(Enum):
    COMPLETE = auto()
    ERROR = auto()
    RUNNING = auto()


def _classify(path: str) -> Status:
    """Classify a single checkpoint leaf directory."""
    simout = os.path.join(path, "simout")
    simerr = os.path.join(path, "simerr")

    if not (os.path.isfile(simout) and os.path.isfile(simerr)):
        return Status.ERROR

    with open(simout) as f:
        out_text = f.read()
    with open(simerr) as f:
        err_text = f.read()

    if any(p.search(out_text) for p in _SUCCESS_PATTERNS):
        return Status.COMPLETE

    if any(p.search(err_text) for p in _ERROR_PATTERNS):
        return Status.ERROR

    return Status.RUNNING


# ═══════════════════════════════════════════════════════════════
#  Result
# ═══════════════════════════════════════════════════════════════

@dataclass
class CheckResult:
    complete: int = 0
    error: int = 0
    total: int = 0
    error_paths: List[str] = field(default_factory=list)
    running_paths: List[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return (self.complete / self.total * 100) if self.total else 0.0

    @property
    def finished(self) -> bool:
        return self.total > 0 and (self.complete + self.error) == self.total

    def as_tuple(self):
        """Backward-compatible (complete, error, total, error_paths)."""
        return self.complete, self.error, self.total, self.error_paths


# ═══════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════

def check_run(path: str) -> CheckResult:
    """
    Walk *path* and classify every leaf directory (no subdirectories)
    as complete / error / running.

    Returns:
        CheckResult with counts and path lists.
    """
    result = CheckResult()

    for root, dirs, files in os.walk(path):
        if dirs:                       # only leaf directories
            continue

        result.total += 1
        status = _classify(root)

        if status is Status.COMPLETE:
            result.complete += 1
            log.debug("complete: %s", root)
        elif status is Status.ERROR:
            result.error += 1
            result.error_paths.append(root)
            log.debug("error:    %s", root)
        else:
            result.running_paths.append(root)
            log.debug("running:  %s", root)

    return result


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check gem5 simulation status")
    parser.add_argument("dir", help="Directory to scan")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show per-directory status")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    r = check_run(args.dir)

    for p in r.error_paths:
        log.warning("error: %s", p)
    for p in r.running_paths:
        log.info("running: %s", p)

    log.info("Complete : %d/%d", r.complete, r.total)
    log.info("Error    : %d/%d", r.error, r.total)
    log.info("Success  : %.2f%%", r.success_rate)