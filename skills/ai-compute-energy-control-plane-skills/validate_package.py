from pathlib import Path
import re, json, sys

ROOT = Path(__file__).resolve().parent
required_root = [
    "README.md", "PACKAGE_MANIFEST.json", "IMPLEMENTATION_ORDER.md",
    "INTERFACE_CATALOG.md", "EVIDENCE_STANDARD.md",
    "CODEX_EXECUTION_PROTOCOL.md", "IMPLEMENTATION_CHECKLIST.md"
]
required_sections = [
    "## 1. Mission", "## 2. Completion truth rule", "## 3. Dependencies",
    "## 4. Inputs", "## 5. Scope and required code modules",
    "## 6. Required interfaces", "## 7. Implementation workflow",
    "## 8. Required tests", "## 9. Required evidence",
    "## 10. Quality and safety gates", "## 11. Definition of Done",
    "## 12. Codex execution prompt", "## 13. Handoff"
]

errors = []
for name in required_root:
    if not (ROOT / name).exists():
        errors.append(f"missing root file: {name}")

batch_dirs = sorted((ROOT / "batches").glob("B??-*"))
if len(batch_dirs) != 20:
    errors.append(f"expected 20 batch directories, found {len(batch_dirs)}")

seen = set()
for d in batch_dirs:
    skill = d / "SKILL.md"
    if not skill.exists():
        errors.append(f"missing {skill}")
        continue
    text = skill.read_text(encoding="utf-8")
    m = re.search(r"^skill_id:\s*(B\d\d)$", text, re.M)
    if not m:
        errors.append(f"{skill}: missing valid skill_id")
    else:
        seen.add(m.group(1))
    for section in required_sections:
        if section not in text:
            errors.append(f"{skill}: missing section {section}")
    if "status: NOT_STARTED" not in text:
        errors.append(f"{skill}: initial status must be NOT_STARTED")
    if "Completion requires" in text and "evidence" not in text.lower():
        errors.append(f"{skill}: completion rule lacks evidence language")

expected = {f"B{i:02d}" for i in range(1, 21)}
if seen != expected:
    errors.append(f"batch ids mismatch: missing={sorted(expected-seen)}, extra={sorted(seen-expected)}")

manifest_path = ROOT / "PACKAGE_MANIFEST.json"
if manifest_path.exists():
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("batch_count") != 20:
        errors.append("manifest batch_count must be 20")

report = ROOT / "VALIDATION_REPORT.md"
if errors:
    report.write_text("# Validation Report\n\nStatus: FAIL\n\n" + "\n".join(f"- {e}" for e in errors) + "\n", encoding="utf-8")
    print("\n".join(errors))
    sys.exit(1)

report.write_text(
    "# Validation Report\n\n"
    "Status: PASS\n\n"
    "- 20 batch directories found.\n"
    "- Every SKILL.md contains all required structural sections.\n"
    "- Batch IDs B01–B20 are present exactly once.\n"
    "- Root package documentation is present.\n\n"
    "> This validates package structure only. It does not claim any production code, integration, optimization, test, security or release gate has been implemented.\n",
    encoding="utf-8",
)
print("Package structure validation: PASS")
