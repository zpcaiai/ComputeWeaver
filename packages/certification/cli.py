from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .evidence import write_evidence
from .lifecycle import CertificationRepository
from .readiness import evaluate_external_readiness
from .service import (
    CertificationResult,
    evaluate_release_from_evidence,
)
from .signing import attach_release_signature, issue_release_token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="certify")
    parser.add_argument(
        "command",
        choices=("preflight", "run", "report", "external-status", "release", "revoke"),
    )
    parser.add_argument("--evidence", type=Path, default=Path("evidence"))
    parser.add_argument("--commit", default="UNVERSIONED")
    parser.add_argument("--release-id", default="local-candidate")
    parser.add_argument("--actor", default="local-operator")
    parser.add_argument("--signing-key", type=Path)
    parser.add_argument("--verification-key", type=Path)
    parser.add_argument("--signing-algorithm", default="ES256")
    parser.add_argument("--key-id")
    parser.add_argument("--reason")
    parser.add_argument("--output", type=Path)
    return parser


def _evaluate(
    evidence_root: Path,
    *,
    release_id: str,
    commit: str,
    generated_at: datetime,
) -> CertificationResult:
    return evaluate_release_from_evidence(
        evidence_root,
        release_id=release_id,
        source_revision=commit,
        generated_at=generated_at,
    )


def _print(document: object) -> None:
    print(json.dumps(document, default=str, indent=2, sort_keys=True))


def main() -> None:
    arguments = _parser().parse_args()
    repository = CertificationRepository(arguments.evidence)
    if arguments.command == "report":
        _print(repository.view(arguments.release_id))
        return
    if arguments.command == "external-status":
        try:
            source_revision = repository.get(arguments.release_id).commit
        except (FileNotFoundError, ValueError):
            source_revision = arguments.commit
        report = evaluate_external_readiness(
            arguments.evidence,
            release_id=arguments.release_id,
            source_revision=source_revision,
        )
        document = report.as_document()
        output = arguments.output or arguments.evidence / "B20" / "external-readiness.json"
        write_evidence(
            output,
            document,
            command="certify external-status",
            suite_name="external-production-readiness",
        )
        _print(document)
        return
    if arguments.command == "revoke":
        if not arguments.reason:
            raise SystemExit("certify revoke requires --reason")
        _print(
            repository.revoke(
                arguments.release_id,
                actor_id=arguments.actor,
                reason=arguments.reason,
            )
        )
        return

    if arguments.command == "release":
        if arguments.signing_key is None:
            raise SystemExit("certify release requires --signing-key")
        if arguments.verification_key is None:
            raise SystemExit("certify release requires --verification-key")
        run = repository.load_run(arguments.release_id)
        if run.commit != arguments.commit:
            raise SystemExit("certification run commit does not match --commit")
        current = _evaluate(
            arguments.evidence,
            release_id=arguments.release_id,
            commit=arguments.commit,
            generated_at=run.generated_at,
        )
        if current.certificate_hash != run.certificate_hash:
            raise SystemExit("certification evidence changed after certify run")
        if current.status != "CERTIFIED":
            _print(asdict(current))
            raise SystemExit(1)
        token = issue_release_token(
            current,
            private_key_file=arguments.signing_key,
            algorithm=arguments.signing_algorithm,
            key_id=arguments.key_id,
        )
        signed = attach_release_signature(current, token)
        repository.publish(
            signed,
            actor_id=arguments.actor,
            public_key_file=arguments.verification_key,
        )
        _print(asdict(signed))
        return

    result = _evaluate(
        arguments.evidence,
        release_id=arguments.release_id,
        commit=arguments.commit,
        generated_at=datetime.now(UTC),
    )
    if arguments.command == "run":
        repository.save_run(result, actor_id=arguments.actor)
    _print(asdict(result))


if __name__ == "__main__":
    main()
