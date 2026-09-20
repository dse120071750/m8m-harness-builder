"""Project dependencies retain one runtime location when native source is staged."""
import importlib

import pytest

import support  # noqa: F401
from flowstep_runtime import FlowError, _assert_closed_repository_imports
from m8m_build_steps import _copy_skill_native_source, _skill_native_source_paths
from project_imports import import_context


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def staged(tmp_path):
    source_root = tmp_path / "source"
    stage = tmp_path / "stage"
    harness = stage / "flowsteps/flows/example"
    dependencies = ["stage_example_shared/action.py", ".agents/skills/example/private_lib/identity.py"]
    handler = "milestones/one/assemble.py"
    write(source_root / dependencies[0], "VALUE = 'captured action'\n")
    write(source_root / dependencies[1], "VALUE = 'captured identity'\n")
    write(source_root / handler,
          "from stage_example_shared.action import VALUE\nfrom private_lib.identity import VALUE as IDENTITY\n")
    write(source_root / "SKILL.md", "# Example source\n")
    source = {"canvas": {"implementation_dependencies": dependencies},
              "resources": [{"path": "SKILL.md"}], "milestones": [{"handler": handler}]}
    written, blockers = _copy_skill_native_source(source_root=source_root, stage=stage,
        harness=harness, skill_name="example", source=source, overwrite=False)
    assert written and blockers == []
    return source_root, stage, harness, source, [harness / handler, *(stage / p for p in dependencies)]


def test_staged_project_imports_do_not_create_shadow_skill_packages(tmp_path):
    source_root, stage, harness, source, frozen = staged(tmp_path)
    roots = [stage, stage / ".agents/skills/example"]
    _assert_closed_repository_imports(stage, frozen, repository_import_roots=roots)
    for relative in _skill_native_source_paths(source):
        assert (harness / relative).read_bytes() == (source_root / relative).read_bytes()
    for skill_root in (stage / ".agents/skills/example", stage / ".claude/skills/example"):
        assert (skill_root / "SKILL.md").is_file()
        assert (skill_root / "milestones/one/assemble.py").is_file()
        assert not (skill_root / "stage_example_shared").exists()
        assert not (skill_root / ".agents").exists()
    with import_context(stage, [".", ".agents/skills/example"], frozen).activate():
        assert importlib.import_module("stage_example_shared.action").VALUE == "captured action"
        assert importlib.import_module("private_lib.identity").VALUE == "captured identity"


def test_staging_does_not_admit_a_genuinely_undeclared_sibling(tmp_path):
    _, stage, _, _, frozen = staged(tmp_path)
    write(stage / "stage_example_shared/undeclared.py", "VALUE = 'unowned'\n")
    with import_context(stage, [".", ".agents/skills/example"], frozen).activate():
        with pytest.raises(ModuleNotFoundError, match="not captured"):
            importlib.import_module("stage_example_shared.undeclared")
    write(frozen[0], "from stage_example_shared.undeclared import VALUE\n")
    with pytest.raises(FlowError, match="outside the frozen implementation closure"):
        _assert_closed_repository_imports(stage, frozen,
            repository_import_roots=[stage, stage / ".agents/skills/example"])


def test_explicit_source_and_project_roles_keep_both_destinations(tmp_path):
    source_root = tmp_path / "source"
    stage = tmp_path / "stage"
    harness = stage / "flowsteps/flows/example"
    write(source_root / "shared.json", '{"both_roles":true}\n')
    source = {"canvas": {"implementation_dependencies": ["shared.json"]},
              "resources": [{"path": "shared.json"}], "milestones": []}
    _, blockers = _copy_skill_native_source(source_root=source_root, stage=stage, harness=harness,
        skill_name="example", source=source, overwrite=False)
    assert blockers == []
    for root in (stage, harness, stage / ".agents/skills/example", stage / ".claude/skills/example"):
        assert (root / "shared.json").read_bytes() == (source_root / "shared.json").read_bytes()
