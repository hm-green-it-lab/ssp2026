# helper/naming.py
"""Artifact naming shared by every tool and host.

Filenames follow the pattern used across the ASE automation::

    {tool}_{experiment_type}_{YYYYMMDD_HHMMSS}_{iteration}_{total}.{ext}
"""

from __future__ import annotations

import re
from datetime import datetime


def ts(fmt: str = "%Y%m%d_%H%M%S") -> str:
    """Return the current local time formatted as a string."""
    return datetime.now().strftime(fmt)


def sanitize(name: str) -> str:
    """Restrict a string to ``[A-Za-z0-9_]``, collapsing everything else."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(name)).strip("_")


def build_filename(
    tool: str,
    experiment_type: str,
    dt_str: str,
    iteration: int,
    total: int,
    ext: str,
) -> str:
    """Build the standard artifact filename.

    >>> build_filename("jmeter", "spring_remote_jagent", "20260817_110131", 1, 3, ".jtl")
    'jmeter_spring_remote_jagent_20260817_110131_1_3.jtl'
    """
    return (
        f"{sanitize(tool)}_{sanitize(experiment_type)}_"
        f"{dt_str}_{int(iteration)}_{int(total)}{ext}"
    )
