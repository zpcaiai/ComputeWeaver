from __future__ import annotations

import argparse
import json


def main() -> None:
    parser = argparse.ArgumentParser(prog="computeweaver")
    parser.add_argument("command", choices=("version", "serve"))
    args = parser.parse_args()
    if args.command == "version":
        print(json.dumps({"name": "computeweaver", "version": "0.1.0"}))
        return
    import uvicorn

    uvicorn.run("apps.api.main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
