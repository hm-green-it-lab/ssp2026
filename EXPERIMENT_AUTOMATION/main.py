# main.py
"""
CLI entrypoint for the eBPF jAgent / OTJAE overhead and accuracy experiments.

Usage
-----
    python -m main --config configuration/spring_remote_jagent.yml
    python -m main --config configuration/spring_remote_otjae.yml --iterations 5

Three machines are involved: this controller runs the automation, a dedicated
load driver runs JMeter, and the SUT runs the Spring REST service together with
the instrumentation under test. Both remote hosts are driven over SSH; unlike
the ASE template the application runs as a plain ``java -jar`` process rather
than in a container.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from orchestrator.runner import run_experiment

HERE = Path(__file__).resolve().parent

# Remote logs and build output are arbitrary UTF-8 -- the jAgent's Makefile
# prints "⟳", for instance -- and a Windows console defaults to cp1252, which
# raises UnicodeEncodeError on the way *out*. Replace rather than crash: losing
# a glyph from a log line must never abort a measurement run.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# SSH credentials live in .env; paths.env is loaded separately for ${VAR}
# substitution inside the YAML configs.
load_dotenv(dotenv_path=HERE / ".env")

# Every experiment this automation knows how to run. The value is the agent
# variant that helper.sut turns into JVM arguments.
EXPERIMENT_TYPES = {
    "spring_remote_none": "none",
    "spring_remote_usdt": "none",
    "spring_remote_noop": "jagent",
    "spring_remote_otjae": "otjae",
    "spring_remote_jagent": "jagent",
    # Control, not a ladder step: both agents on one JVM, to show that a CPU
    # comparison taken within a single run cannot see the probe overhead
    # because both tools read the same kernel counter. Keep it out of the
    # randomised campaign -- see configuration/spring_remote_both.yml.
    "spring_remote_both": "both",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Run one eBPF jAgent / OTJAE experiment configuration."
    )
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="Override experiment.iterations from config",
    )
    parser.add_argument(
        "--total-rate",
        type=int,
        default=None,
        help="Override jmeter.props.total_rate from config",
    )
    parser.add_argument(
        "--repetition",
        type=int,
        default=None,
        help="Campaign repetition (block) this run belongs to; recorded in the "
             "output folder name and the manifest so blocks can be paired later",
    )
    parser.add_argument(
        "--position",
        type=int,
        default=None,
        help="Position of this run within its repetition, for checking residual "
             "order effects",
    )
    parser.add_argument(
        "--skip-downloads",
        action="store_true",
        help="Do not fetch missing JMX/agent JARs; fail if they are absent",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve the config and print the commands, then exit",
    )
    return parser.parse_args(argv)


def load_env_file(path: Path) -> dict:
    """Parse a ``KEY=VALUE`` env file into a dict.

    Blank lines and ``#`` comments are skipped; only the first ``=`` splits a
    line, so values may themselves contain ``=``.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def substitute_env_vars(text: str, env: dict) -> str:
    """Replace every ``${VAR}`` occurrence in *text* with its value from *env*.

    Placeholders without a matching key are left verbatim rather than expanding
    to an empty string, so a typo stays visible as a literal ``${TYPO}`` in the
    failing command instead of silently producing a truncated path.
    """
    pattern = re.compile(r"\$\{(\w+)\}")

    def replace(match: re.Match) -> str:
        """Look up one placeholder, falling back to the unchanged text."""
        return env.get(match.group(1), match.group(0))

    return pattern.sub(replace, text)


def _load_yaml(path: Path) -> dict:
    """Read one YAML file, substituting ``${VAR}`` placeholders from paths.env."""
    env_values = load_env_file(HERE / "paths.env")
    raw = path.read_text(encoding="utf-8")
    return yaml.safe_load(substitute_env_vars(raw, env_values)) or {}


def merge_configs(base_config: dict, extension_config: dict) -> dict:
    """Recursively merge *extension_config* onto a copy of *base_config*.

    Nested mappings merge key by key; every other value (lists included)
    replaces the base entry wholesale, so the extending config always wins.
    """

    def deep_merge(source: dict, destination: dict) -> dict:
        for key, value in source.items():
            if (
                key in destination
                and isinstance(destination[key], dict)
                and isinstance(value, dict)
            ):
                deep_merge(value, destination[key])
            else:
                destination[key] = value
        return destination

    return deep_merge(extension_config, base_config.copy())


def load_config(path: str, _visited: set[Path] | None = None) -> dict:
    """Load a YAML config, following any ``extends`` chain."""
    config_path = Path(path).resolve()

    if _visited is None:
        _visited = set()
    if config_path in _visited:
        raise ValueError(f"Circular config inheritance detected at: {config_path}")
    _visited.add(config_path)

    config = _load_yaml(config_path)
    if not isinstance(config, dict):
        raise ValueError(f"Config file must contain a mapping at top level: {config_path}")

    base_ref = config.pop("extends", None)
    if not base_ref:
        return config

    if not isinstance(base_ref, str):
        raise ValueError("The `extends` value must be a path string.")

    base_path = Path(base_ref)
    if not base_path.is_absolute():
        base_path = (config_path.parent / base_path).resolve()
    if not base_path.exists():
        raise FileNotFoundError(
            f"Base config referenced by `extends` was not found: '{base_ref}' "
            f"(from {config_path})"
        )

    return merge_configs(load_config(str(base_path), _visited=_visited), config)


def apply_cli_overrides(config: dict, args: argparse.Namespace) -> dict:
    """Apply runtime overrides from CLI flags without mutating the YAML files."""
    if args.iterations is not None:
        config.setdefault("experiment", {})["iterations"] = args.iterations
        print(f"[CLI] Overriding experiment.iterations={args.iterations}")

    if args.total_rate is not None:
        config.setdefault("jmeter", {}).setdefault("props", {})["total_rate"] = args.total_rate
        print(f"[CLI] Overriding jmeter.props.total_rate={args.total_rate}")

    return config


def main(argv: list[str] | None = None) -> None:
    """Load one experiment configuration and run it."""
    args = parse_args(argv)
    config = apply_cli_overrides(load_config(args.config), args)

    experiment_type = config.get("experiment", {}).get("type")
    if experiment_type not in EXPERIMENT_TYPES:
        raise ValueError(
            f"Unknown experiment type: {experiment_type!r}. "
            f"Known types: {sorted(EXPERIMENT_TYPES)}"
        )

    run_experiment(
        config,
        experiment_type=experiment_type,
        skip_downloads=args.skip_downloads,
        dry_run=args.dry_run,
        repetition=args.repetition,
        position=args.position,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
