import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _release_workflow() -> str:
    return (PROJECT_ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8",
    )


def _job(workflow: str, name: str, next_name: str | None = None) -> str:
    job = workflow.split(f"\n  {name}:\n", maxsplit=1)[1]
    if next_name is not None:
        job = job.split(f"\n  {next_name}:\n", maxsplit=1)[0]
    return job


def test_release_build_repeats_security_and_distribution_gates() -> None:
    workflow = _release_workflow()
    build = _job(workflow, "build", "test")

    lock_check = build.index("uv lock --check")
    dependency_sync = build.index("uv sync --frozen --no-install-project")
    frozen_export = build.index("uv export --frozen --no-emit-project")
    dependency_audit = build.index("uv run --no-sync pip-audit --strict")
    audit_input = build.index('--requirement "$RUNNER_TEMP/audit-requirements.txt"')
    project_sync = build.index("uv sync --frozen --no-editable --no-build-isolation")
    quality_gates = build.index("uv run --no-sync ruff format --check .")
    dead_code_gate = build.index("uv run --no-sync vulture")
    installed_package_tests = build.index(
        "uv run --no-sync pytest --cov=pydantic_versions --cov-report=term",
    )
    build_command = "uv build --python .venv/bin/python --no-build-isolation"
    package_build = build.index(build_command)
    metadata_check = build.index("uv run --no-sync twine check --strict dist/*")
    artifact_upload = build.index("actions/upload-artifact@")

    assert (
        lock_check
        < dependency_sync
        < frozen_export
        < dependency_audit
        < audit_input
        < project_sync
        < quality_gates
        < dead_code_gate
        < installed_package_tests
        < package_build
        < metadata_check
        < artifact_upload
    )
    assert "pytest --cov=src" not in build
    assert "run: uv python install\n" in build
    assert "uv python install 3.12" not in build
    assert "continue-on-error" not in build
    sync_commands = [
        line.strip().removeprefix("run: ")
        for line in build.splitlines()
        if line.strip().startswith(("uv sync ", "run: uv sync "))
    ]
    assert sync_commands == [
        "uv sync --frozen --no-install-project",
        "uv sync --frozen --no-editable --no-build-isolation",
    ]
    assert build.count("uv run ") == build.count("uv run --no-sync ")
    assert build.count("uv build ") == build.count(build_command)


def test_dead_code_gate_scans_production_with_local_exceptions() -> None:
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    vulture = pyproject["tool"]["vulture"]

    assert "vulture>=2.16" in pyproject["dependency-groups"]["dev"]
    assert vulture == {
        "min_confidence": 60,
        "paths": ["src"],
        "sort_by_size": True,
    }

    vulture_exceptions = {
        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{line.strip()}"
        for path in (PROJECT_ROOT / "src").rglob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
        if "# noqa: V" in line
    }
    assert vulture_exceptions == {
        "src/pydantic_versions/_wire.py:adapter.__pydantic_computed_fields__ = dict(  "
        "# noqa: V101 - Pydantic reads this",
        "src/pydantic_versions/_wire.py:copied.alias_priority = 2  "
        "# noqa: V101 - Pydantic reads copied metadata",
        "src/pydantic_versions/_wire.py:def __init__(self, /, **data: Any) -> None:  "
        "# noqa: V103 - Pydantic entry point",
        "src/pydantic_versions/family.py:def defaults_for(  # noqa: V105 - public consumer API",
        "src/pydantic_versions/family.py:def describe(self) -> SchemaInventory:  "
        "# noqa: V105 - public consumer API",
        "src/pydantic_versions/family.py:def plan_validation(self, source_version: str) -> "
        "ConversionPlan:  # noqa: V105 - public API",
    }

    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    assert makefile.count("\tuv run vulture\n") == 3

    ci_workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(
        encoding="utf-8",
    )
    assert ci_workflow.count("run: uv run vulture") == 1
    assert _release_workflow().count("uv run --no-sync vulture") == 1


def test_release_uses_the_locked_build_backend_after_the_audit() -> None:
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))

    hatchling_requirement = "hatchling>=1.31.0"
    assert pyproject["build-system"]["requires"] == [hatchling_requirement]
    assert hatchling_requirement in pyproject["dependency-groups"]["dev"]
    assert sum(package["name"] == "hatchling" for package in lock["package"]) == 1


def test_release_tags_must_point_to_default_branch_history() -> None:
    build = _job(_release_workflow(), "build", "test")
    checkout = build.split("- name: Check out source", maxsplit=1)[1].split(
        "- name: Verify tagged commit",
        maxsplit=1,
    )[0]
    lineage = build.split(
        "- name: Verify tagged commit is on the default branch",
        maxsplit=1,
    )[1].split("- name: Install uv", maxsplit=1)[0]

    assert "fetch-depth: 0" in checkout
    assert "if: github.event_name == 'push'" in lineage
    assert "DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}" in lineage
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" "origin/$DEFAULT_BRANCH"' in lineage


def test_manual_release_rehearsal_does_not_publish_by_default() -> None:
    workflow = _release_workflow()
    dispatch_inputs = workflow.split("  workflow_dispatch:", maxsplit=1)[1].split(
        "\n\npermissions:",
        maxsplit=1,
    )[0]
    testpypi_job = _job(workflow, "publish-testpypi", "publish-pypi")

    assert (
        """      publish_testpypi:
        description: "Publish validated artifacts to TestPyPI"
        required: false
        default: false
        type: boolean"""
        in dispatch_inputs
    )
    assert "needs: [build, test]" in testpypi_job
    assert "github.event_name == 'workflow_dispatch'" in testpypi_job
    assert "github.ref_name == github.event.repository.default_branch" in testpypi_job
    assert "inputs.publish_testpypi == true" in testpypi_job


def test_production_publish_remains_tag_only_and_precedes_the_github_release() -> None:
    workflow = _release_workflow()
    pypi_job = _job(workflow, "publish-pypi", "github-release")
    github_release_job = _job(workflow, "github-release")

    assert "needs: [build, test]" in pypi_job
    assert "if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')" in pypi_job
    assert "workflow_dispatch" not in pypi_job
    assert "needs: [build, publish-pypi]" in github_release_job


def test_commit_message_checkout_does_not_persist_credentials() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/commit-messages.yml").read_text(
        encoding="utf-8",
    )
    checkout = workflow.index("uses: actions/checkout@")
    validation_step = workflow.index("- name: Validate PR title and commits")

    assert (
        "group: commit-messages-${{ github.event.pull_request.number || github.ref }}" in workflow
    )
    assert "persist-credentials: false" in workflow[checkout:validation_step]
    assert "github.event.repository.default_branch" in workflow[checkout:validation_step]
    assert "github.event.pull_request.head.sha" not in workflow[checkout:validation_step]
