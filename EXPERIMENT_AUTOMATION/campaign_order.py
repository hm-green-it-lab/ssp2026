"""
campaign_order.py

Emit the variant order for one repetition of a campaign.

Within a repetition the variants must not always run in the same sequence. If
they did, run position would be perfectly confounded with variant -- the
baseline always coldest and first, the last variant always ~30 minutes into a
sustained-load session -- and any monotone drift (host thermals, frequency
scaling, accumulated page cache) would land entirely on the instrumented
variants and be indistinguishable from instrumentation overhead.

Interleaving the variants spreads *between*-repetition drift; randomising their
order spreads *within*-repetition drift. Both are needed.

The order is derived from ``seed`` and ``repetition``, so a campaign is
reproducible: the same seed replays the same sequence. ``--seed 0`` draws a
fresh random order instead.

Usage::

    python campaign_order.py --seed 42 --repetition 3 cfg_a cfg_b cfg_c
"""

from __future__ import annotations

import argparse
import random


def latin_order(configs: list[str], seed: int, repetition: int) -> list[str]:
    """Return one row of a cyclic Latin square.

    Free randomisation leaves residual imbalance at the sample sizes used here:
    over 10 repetitions of 5 variants it is entirely possible for a variant
    never to occupy some position, which is the very confound the shuffling was
    meant to remove. A Latin square guarantees that across each group of *n*
    repetitions every variant occupies every position exactly once.

    The square itself is randomised per replicate, so the design is balanced
    without being a fixed rotation.
    """
    n = len(configs)
    replicate, row = divmod(repetition - 1, n)

    rng = random.Random(None if seed == 0 else seed * 7919 + replicate)
    base = list(configs)
    rng.shuffle(base)

    return [base[(row + position) % n] for position in range(n)]


def random_order(configs: list[str], seed: int, repetition: int) -> list[str]:
    """Return a freely shuffled order for one repetition."""
    rng = random.Random(None if seed == 0 else seed * 1000 + repetition)
    order = list(configs)
    rng.shuffle(order)
    return order


def main() -> None:
    """Print the variant order for one repetition."""
    parser = argparse.ArgumentParser(description="Emit a variant order for one repetition.")
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Campaign seed; 0 draws a fresh order each time (default: 0)",
    )
    parser.add_argument("--repetition", type=int, required=True, help="Repetition number")
    parser.add_argument(
        "--design",
        choices=("latin", "random"),
        default="latin",
        help="latin: balanced Latin square (default); random: free shuffle",
    )
    parser.add_argument("configs", nargs="+", help="Variant names to order")
    args = parser.parse_args()

    order = (
        latin_order(args.configs, args.seed, args.repetition)
        if args.design == "latin"
        else random_order(args.configs, args.seed, args.repetition)
    )
    print(" ".join(order))


if __name__ == "__main__":
    main()
