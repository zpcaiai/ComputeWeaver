.PHONY: bootstrap contracts contracts-check lint typecheck test test-integration verify dev migrate seed evidence clean web-build container-verify docker-inspect docker-clean production-preflight evidence-request external-acceptance external-gate-suite production-load restore-rehearsal issue-attestation verify-attestations

PYTHON ?= .venv/bin/python
PIP ?= .venv/bin/pip
EVIDENCE_REQUEST ?= evidence/B20/evidence-request.json

bootstrap:
	python3 -m venv .venv
	$(PIP) install -r requirements.lock
	$(PIP) install -e . --no-deps --no-build-isolation
	npm --prefix apps/web ci

contracts:
	$(PYTHON) scripts/export_contracts.py

contracts-check:
	$(PYTHON) scripts/export_contracts.py --check

lint:
	.venv/bin/ruff check apps packages tests scripts

typecheck:
	.venv/bin/mypy apps packages scripts

test:
	$(PYTHON) -m pytest -m "not integration" --junitxml=evidence/test-results.xml --cov --cov-report=xml:evidence/coverage.xml
	$(PYTHON) -m scripts.record_test_run --junit evidence/test-results.xml --coverage evidence/coverage.xml --output evidence/test-run-binding.json --suite-name unit-and-contract-tests

test-integration:
	@test -n "$$COMPUTEWEAVER_TEST_DATABASE_URL" || (echo "COMPUTEWEAVER_TEST_DATABASE_URL is required" && exit 2)
	$(PYTHON) -m pytest -m integration -vv --junitxml=evidence/postgres-integration.xml
	$(PYTHON) -m scripts.record_test_run --junit evidence/postgres-integration.xml --output evidence/postgres-integration-binding.json --suite-name postgres-integration-tests

web-build:
	npm --prefix apps/web run build

verify: lint typecheck contracts-check test web-build
	$(PYTHON) skills/ai-compute-energy-control-plane-skills/validate_package.py
	$(PYTHON) scripts/validate_repo.py
	$(PYTHON) scripts/verify_ci_negative_gate.py

dev:
	COMPUTEWEAVER_ENV=simulator COMPUTEWEAVER_DATABASE_URL=memory:// COMPUTEWEAVER_AUTH_MODE=trusted_headers \
		$(PYTHON) -m uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8000

migrate:
	$(PYTHON) -m packages.persistence.cli migrate

seed:
	$(PYTHON) scripts/seed.py

evidence:
	$(PYTHON) -m scripts.generate_evidence

container-verify:
	$(PYTHON) -m scripts.build_containers $(if $(IMAGE_BUNDLE),--image-bundle "$(IMAGE_BUNDLE)",) --output evidence/B01/container-build-result.json

docker-inspect:
	$(PYTHON) -m scripts.project_docker inspect

docker-clean:
	@test "$$PROJECT_DOCKER_APPLY" = "1" || (echo "PROJECT_DOCKER_APPLY=1 is required" && exit 2)
	$(PYTHON) -m scripts.project_docker clean --apply

production-preflight:
	@test -n "$$PRODUCTION_PREFLIGHT_CONFIG" || (echo "PRODUCTION_PREFLIGHT_CONFIG is required" && exit 2)
	$(PYTHON) -m scripts.run_production_preflight "$$PRODUCTION_PREFLIGHT_CONFIG"

evidence-request:
	@test -n "$$EVIDENCE_REQUEST_CONFIG" || (echo "EVIDENCE_REQUEST_CONFIG is required" && exit 2)
	$(PYTHON) -m scripts.run_production_gates request "$$EVIDENCE_REQUEST_CONFIG" --output "$(EVIDENCE_REQUEST)"

external-acceptance:
	@test -n "$$ACCEPTANCE_MANIFEST" || (echo "ACCEPTANCE_MANIFEST is required" && exit 2)
	$(PYTHON) -m scripts.run_external_acceptance "$$ACCEPTANCE_MANIFEST" --request "$(EVIDENCE_REQUEST)"

external-gate-suite:
	@test -n "$$EXTERNAL_GATE_SUITE_CONFIG" || (echo "EXTERNAL_GATE_SUITE_CONFIG is required" && exit 2)
	$(PYTHON) -m scripts.run_external_gate_suite "$$EXTERNAL_GATE_SUITE_CONFIG"

production-load:
	@test -n "$$LOAD_TARGET" -a -n "$$RELEASE_ID" -a -n "$$SOURCE_REVISION" -a -n "$$LOAD_TOKEN_REF" || (echo "LOAD_TARGET, RELEASE_ID, SOURCE_REVISION and LOAD_TOKEN_REF are required" && exit 2)
	$(PYTHON) -m scripts.run_production_gates load "$$LOAD_TARGET" --requests "$${LOAD_REQUESTS:-1000}" --concurrency "$${LOAD_CONCURRENCY:-25}" --p95-ms "$${LOAD_P95_MS:-300}" --p99-ms "$${LOAD_P99_MS:-1000}" --max-error-rate "$${LOAD_MAX_ERROR_RATE:-0.001}" --release-id "$$RELEASE_ID" --source-revision "$$SOURCE_REVISION" --request "$(EVIDENCE_REQUEST)" --token-ref "$$LOAD_TOKEN_REF"

restore-rehearsal:
	@test -n "$$RESTORE_CONFIG" || (echo "RESTORE_CONFIG is required" && exit 2)
	$(PYTHON) -m scripts.run_restore_rehearsal "$$RESTORE_CONFIG" --request "$(EVIDENCE_REQUEST)"

issue-attestation:
	@test -n "$$ATTESTATION_SIGN_CONFIG" -a -n "$$ATTESTATION_OUTPUT" || (echo "ATTESTATION_SIGN_CONFIG and ATTESTATION_OUTPUT are required" && exit 2)
	$(PYTHON) -m scripts.run_production_gates sign "$$ATTESTATION_SIGN_CONFIG" --output "$$ATTESTATION_OUTPUT"

verify-attestations:
	@test -n "$$ATTESTATION_BUNDLE" -a -n "$$ATTESTATION_POLICY" || (echo "ATTESTATION_BUNDLE and ATTESTATION_POLICY are required" && exit 2)
	$(PYTHON) -m scripts.run_production_gates attestations "$$ATTESTATION_BUNDLE" --request "$(EVIDENCE_REQUEST)" --policy "$$ATTESTATION_POLICY"

clean:
	$(PYTHON) scripts/clean.py
