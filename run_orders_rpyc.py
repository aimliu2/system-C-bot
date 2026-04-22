"""System C V2 RPyC MT5 entrypoint."""

from __future__ import annotations

from runtime.adapters import RpycMt5Adapter
from runtime.config import load_runtime_config
from runtime.runner import build_common_parser, run_with_adapter


def main() -> int:
    parser = build_common_parser("System C V2 RPyC MT5 runner")
    args = parser.parse_args()
    cfg = load_runtime_config()
    adapter = None if args.dry_run else RpycMt5Adapter(cfg)
    run_with_adapter(adapter, dry_run=args.dry_run, once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
