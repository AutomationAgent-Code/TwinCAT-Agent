from pathlib import Path
import xml.etree.ElementTree as ET

import yaml

from tc_template.case_library import discover_cases, find_cases, get_case


HAM_ROOT = Path(__file__).resolve().parents[1] / "CaseLibrary" / "HAM"


def test_ham_collection_and_case_manifests_are_valid():
    collection = yaml.safe_load((HAM_ROOT / "collection.yaml").read_text(encoding="utf-8"))
    assert collection["id"] == "HAM"

    manifests = sorted(HAM_ROOT.rglob("manifest.yaml"))
    assert len(manifests) >= 6
    ids = set()
    for path in manifests:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["id"].startswith("ham.")
        assert data["id"] not in ids
        ids.add(data["id"])
        entry = path.parent / data["entry"]
        assert entry.is_file(), entry
        ET.parse(entry)


def test_ham_cases_are_grouped_by_declared_category():
    for path in HAM_ROOT.rglob("manifest.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        relative = path.relative_to(HAM_ROOT)
        assert relative.parts[0] == data["category"]


def test_case_library_discovers_and_finds_ham_cases():
    cases = discover_cases()
    assert len([case for case in cases if case["publisher"] == "HAM"]) >= 6
    assert find_cases("EL6001 串口")[0]["id"] == "ham.communication.serial-el6001"
    assert get_case("HAM.MOTION.MOTOR-AXIS")["name"] == "HAM Motor Axis Wrapper"


def test_customer_higher_version_overrides_builtin(tmp_path):
    case_dir = tmp_path / "communication" / "serial-el6001"
    case_dir.mkdir(parents=True)
    (case_dir / "case.xml").write_text("<project />", encoding="utf-8")
    (case_dir / "manifest.yaml").write_text(
        "\n".join([
            "id: ham.communication.serial-el6001",
            "name: Customer Override",
            "version: '9.0.0'",
            "publisher: HAM",
            "category: communication",
            "type: plcopen",
            "entry: case.xml",
            "objects: []",
        ]), encoding="utf-8")
    cases = discover_cases(extra_roots=[tmp_path])
    selected = next(case for case in cases
                    if case["id"] == "ham.communication.serial-el6001")
    assert selected["name"] == "Customer Override"
