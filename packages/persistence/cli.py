from __future__ import annotations

import argparse
import json

from config.settings import Settings

from .postgres import PostgresRuntime


def main() -> None:
    parser = argparse.ArgumentParser(description="ComputeWeaver PostgreSQL lifecycle")
    parser.add_argument("command", choices=("migrate", "check"))
    arguments = parser.parse_args()
    settings = Settings.from_env()
    if settings.in_memory_mode:
        raise SystemExit("PostgreSQL lifecycle commands reject memory mode")
    runtime = PostgresRuntime(
        settings.database_url,
        min_size=1,
        max_size=max(1, settings.database_pool_max),
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    try:
        if arguments.command == "migrate":
            result = {"applied": runtime.migrate(), "healthy": runtime.health()}
        else:
            result = {"healthy": runtime.health()}
        print(json.dumps(result))
        if not result["healthy"]:
            raise SystemExit(1)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
