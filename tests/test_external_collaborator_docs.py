from pathlib import Path


README = Path("README.md")
CHECKLIST = Path("docs/external_collaborator_demo_checklist.md")
DOC_PATHS = [
    README,
    CHECKLIST,
    Path("docs/architecture.md"),
    Path("docs/demo_flow.md"),
    Path("docs/demo_participation_boundary.md"),
    Path("docs/legal_safety_notes.md"),
    Path("docs/realtime_design.md"),
]


def _read(path: Path) -> str:
    return path.read_text()


def test_external_collaborator_demo_checklist_exists():
    text = _read(CHECKLIST)

    for phrase in [
        "External Collaborator Demo Checklist",
        "DEMO_ADMIN_TOKEN",
        "DEMO_COOKIE_SECURE",
        "DEMO_PREDICTION_MAX_DEMO_STAKE",
        "participant codes",
        "Do not share `/admin/audit`",
        "Back up the SQLite database",
        "Audit CSV",
        "Result Confirmation Checks",
        "data will be deleted or retained",
    ]:
        assert phrase in text


def test_readme_links_external_collaborator_operations():
    text = _read(README)

    assert "External collaborator demo operations" in text
    assert "docs/external_collaborator_demo_checklist.md" in text
    assert "DEMO_ADMIN_TOKEN" in text
    assert "DEMO_COOKIE_SECURE" in text
    assert "DEMO_PREDICTION_MAX_DEMO_STAKE" in text
    assert "/admin/audit.csv" in text


def test_docs_keep_non_exchangeable_demo_point_boundary():
    combined = "\n".join(_read(path) for path in [README, CHECKLIST])

    assert "non-cash" in combined
    assert "non-transferable" in combined
    assert "non-exchangeable" in combined
    assert "換金" in combined
    assert "譲渡" in combined
    assert "交換" in combined


def test_docs_do_not_reintroduce_old_label_or_port():
    forbidden = ["M" + "V" + "P", "m" + "v" + "p", "80" + "92"]
    combined = "\n".join(_read(path) for path in DOC_PATHS)

    for term in forbidden:
        assert term not in combined
