from __future__ import annotations

import argparse
import json
from pathlib import Path

from packages.certification.images import bundle_document, export_image_bundle, verify_image_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Export digest-pinned Docker images for offline transfer")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--reference", action="append", required=True)
    parser.add_argument("--minimum-free-gib", type=float, default=8)
    arguments = parser.parse_args()
    manifest = export_image_bundle(
        arguments.reference,
        arguments.destination,
        minimum_free_gib=arguments.minimum_free_gib,
    )
    result = {"status": "PASS", **bundle_document(verify_image_bundle(manifest))}
    print(json.dumps(result, indent=2, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
