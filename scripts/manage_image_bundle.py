from __future__ import annotations

import argparse
import json
from pathlib import Path

from packages.certification.images import bundle_document, load_image_bundle, verify_image_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify or load a digest-pinned offline Docker image bundle")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--load", action="store_true", help="load the verified images into Docker")
    arguments = parser.parse_args()
    if arguments.load:
        result = load_image_bundle(arguments.manifest)
        status = result["status"]
    else:
        try:
            result = {"status": "PASS", **bundle_document(verify_image_bundle(arguments.manifest))}
            status = "PASS"
        except (OSError, ValueError, json.JSONDecodeError) as error:
            result = {"status": "FAIL", "reason": str(error)}
            status = "FAIL"
    print(json.dumps(result, indent=2, default=str, sort_keys=True))
    raise SystemExit(0 if status == "PASS" else 2)


if __name__ == "__main__":
    main()
