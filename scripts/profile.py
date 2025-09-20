from __future__ import annotations

import argparse
import cProfile
from pathlib import Path

from scripts.bench import benchmark_formatter, benchmark_processing


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile benchmark scenarios.")
    parser.add_argument(
        "target",
        choices=("formatter", "processing"),
        help="Benchmark target to profile",
    )
    parser.add_argument("--iterations", type=int, default=500, help="Iterations to run")
    args = parser.parse_args()

    profiles_dir = Path("profiles")
    profiles_dir.mkdir(parents=True, exist_ok=True)
    profile_path = profiles_dir / f"{args.target}.prof"

    if args.target == "formatter":
        runner = benchmark_formatter
    else:
        runner = benchmark_processing

    def run() -> None:
        runner(args.iterations)

    cProfile.runctx("run()", globals(), locals(), str(profile_path))
    print(f"Profile written to {profile_path}")


if __name__ == "__main__":
    main()
