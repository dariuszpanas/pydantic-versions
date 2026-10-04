"""Exercise the repository's YAGA policy through its installed CLI."""

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / ".yaga.toml"
DEPENDABOT: dict[str, object] = {"login": "dependabot[bot]", "id": 49699333, "type": "Bot"}
DEPENDABOT_REF = "dependabot/uv/python-security-662a8f1785"
VALID_BODY = "Preserve schema compatibility while updating our development tooling safely."

# Exact raw commit message from the failed Dependabot pull request:
# https://github.com/dariuszpanas/pydantic-versions/pull/152
# commit 36eb7981c12dbb4e718cbfca967d091399b56d9c (not GraphQL's shortened headline).
DEPENDABOT_MESSAGE = """\
chore(deps): bump the python-security group across 1 directory with 2 updates

Bumps the python-security group with 2 updates in the / directory: [django](https://github.com/django/django) and [urllib3](https://github.com/urllib3/urllib3).


Updates `django` from 6.1 to 6.1.1
- [Commits](https://github.com/django/django/compare/6.1...6.1.1)

Updates `urllib3` from 2.7.0 to 2.8.0
- [Release notes](https://github.com/urllib3/urllib3/releases)
- [Changelog](https://github.com/urllib3/urllib3/blob/main/CHANGES.rst)
- [Commits](https://github.com/urllib3/urllib3/compare/2.7.0...2.8.0)

---
updated-dependencies:
- dependency-name: django
  dependency-version: 6.1.1
  dependency-type: indirect
  dependency-group: python-security
- dependency-name: urllib3
  dependency-version: 2.8.0
  dependency-type: indirect
  dependency-group: python-security
...

Signed-off-by: dependabot[bot] <support@github.com>
"""


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _repository(
    tmp_path: Path, message: str, *, weaken_policy: bool = False
) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    # A Git author is freely editable and cannot authenticate Dependabot.
    _git(repository, "config", "user.name", "dependabot[bot]")
    _git(repository, "config", "user.email", "49699333+dependabot[bot]@users.noreply.github.com")
    _git(repository, "config", "commit.gpgsign", "false")
    _git(repository, "config", "core.hooksPath", str(tmp_path / "disabled-hooks"))
    (repository / ".yaga.toml").write_bytes(POLICY.read_bytes())
    _git(repository, "add", "--", ".yaga.toml")
    _git(repository, "commit", "-m", "chore: establish baseline")
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-b", "pr-candidate")
    if weaken_policy:
        (repository / ".yaga.toml").write_text(
            'config-version = 1\n[commit]\nbody-policy = "optional"\n', encoding="utf-8"
        )
        _git(repository, "add", "--", ".yaga.toml")
    _git(repository, "commit", "--allow-empty", "-m", message)
    head = _git(repository, "rev-parse", "HEAD")
    # Match the workflow: fetched PR objects, but the runner checks out the base.
    _git(repository, "update-ref", "refs/remotes/pull/152/head", head)
    _git(repository, "checkout", "--detach", base)
    return repository, base, head


def _event(
    tmp_path: Path,
    base: str,
    head: str,
    author: Mapping[str, object],
    *,
    head_ref: str = DEPENDABOT_REF,
    head_repository: Mapping[str, object] | None = None,
) -> Path:
    repository = {
        "id": 1241727208,
        "full_name": "dariuszpanas/pydantic-versions",
        "default_branch": "main",
    }
    event = {
        "action": "synchronize",
        "number": 152,
        "repository": repository,
        "pull_request": {
            "number": 152,
            "title": DEPENDABOT_MESSAGE.splitlines()[0],
            "state": "open",
            "draft": False,
            "user": dict(author),
            "base": {"sha": base, "ref": "main", "repo": repository},
            "head": {
                "sha": head,
                "ref": head_ref,
                "repo": dict(head_repository) if head_repository is not None else repository,
            },
        },
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    return path


def _yaga(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "yaga", *arguments, "--config", str(POLICY), "--format", "json"],
        cwd=ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("YAGA_", "GITHUB_"))
    }


def _action(
    repository: Path, base: str, event: Path, *, head_ref: str = DEPENDABOT_REF
) -> subprocess.CompletedProcess[str]:
    environment = _environment()
    environment.update(
        {
            "YAGA_COMMIT_ACTION_RUNTIME": "1",
            "YAGA_COMMIT_TRUSTED_CONFIG": ".yaga.toml",
            "GITHUB_EVENT_NAME": "pull_request_target",
            "GITHUB_REPOSITORY": "dariuszpanas/pydantic-versions",
            "GITHUB_REPOSITORY_ID": "1241727208",
            "GITHUB_BASE_REF": "main",
            "GITHUB_HEAD_REF": head_ref,
            "GITHUB_SHA": base,
            "GITHUB_REF": "refs/heads/main",
        }
    )
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "yaga",
            "github",
            "pull-request",
            "check",
            "--event-file",
            str(event),
            "--repo",
            str(repository),
            "--format",
            "github",
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_release_commit_accepts_a_proportional_body_without_legacy_sections() -> None:
    result = _yaga(
        "commit", "check", "--message", f"release: publish stable package\n\n{VALID_BODY}"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["valid"] is True
    assert report["passed"] == 1


@pytest.mark.parametrize(
    ("message", "diagnostic"),
    [
        ("fix: preserve schema compatibility", "body.required"),
        (
            "fix: preserve schema compatibility\n\nThis body has only seven words total.",
            "body.word-count",
        ),
        (f"Fix: preserve schema compatibility\n\n{VALID_BODY}", "type.case"),
        (f"fix(API): preserve schema compatibility\n\n{VALID_BODY}", "scope.case"),
        (f"fix: {'x' * 96}\n\n{VALID_BODY}", "header.length"),
    ],
)
def test_human_commit_policy_remains_enforced(message: str, diagnostic: str) -> None:
    result = _yaga("commit", "check", "--message", message)

    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert diagnostic in {item["code"] for item in report["commits"][0]["diagnostics"]}


def test_failed_dependabot_pr_is_accepted_with_an_explicit_skip_reason(tmp_path: Path) -> None:
    repository, base, head = _repository(tmp_path, DEPENDABOT_MESSAGE)
    event = _event(tmp_path, base, head, DEPENDABOT)

    result = _action(repository, base, event)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "pull request #152: one title and 1 commit(s)" in result.stdout
    assert "0 failed, 2 skipped" in result.stdout
    assert "Skip reason: Dependabot pull request" in result.stdout


@pytest.mark.parametrize(
    ("author", "head_ref", "head_repository"),
    [
        ({"login": "contributor", "id": 1, "type": "User"}, DEPENDABOT_REF, None),
        ({"login": "dependabot[bot]", "id": 49699333, "type": "User"}, DEPENDABOT_REF, None),
        ({"login": "dependabot", "id": 49699333, "type": "Bot"}, DEPENDABOT_REF, None),
        (DEPENDABOT, "feat/dependency-update", None),
        (
            DEPENDABOT,
            DEPENDABOT_REF,
            {"id": 2, "full_name": "contributor/pydantic-versions"},
        ),
    ],
    ids=["human", "fake-account-type", "fake-login", "ordinary-branch", "fork"],
)
def test_untrusted_identity_cannot_skip_or_weaken_commit_policy(
    tmp_path: Path,
    author: Mapping[str, object],
    head_ref: str,
    head_repository: Mapping[str, object] | None,
) -> None:
    repository, base, head = _repository(
        tmp_path, "fix: preserve schema compatibility", weaken_policy=True
    )
    event = _event(tmp_path, base, head, author, head_ref=head_ref, head_repository=head_repository)
    # Also corrupt the working copy: only the committed runner-base policy is trusted.
    (repository / ".yaga.toml").write_text("not valid TOML", encoding="utf-8")

    result = _action(repository, base, event, head_ref=head_ref)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 failed, 0 skipped" in result.stdout
    assert "[body.required]" in result.stdout
    assert head[:12] in result.stdout


@pytest.mark.parametrize("mismatch", ["runner-checkout", "fetched-pr-head"])
def test_dependabot_skip_still_rejects_mismatched_commits(tmp_path: Path, mismatch: str) -> None:
    repository, base, head = _repository(tmp_path, DEPENDABOT_MESSAGE)
    event = _event(tmp_path, base, head, DEPENDABOT)
    if mismatch == "runner-checkout":
        _git(repository, "checkout", "--detach", head)
        expected = "trusted checkout HEAD does not match the runner SHA"
    else:
        _git(repository, "update-ref", "refs/remotes/pull/152/head", base)
        expected = "fetched pull request head does not match the event head SHA"

    result = _action(repository, base, event)

    assert result.returncode == 2, result.stdout + result.stderr
    assert expected in result.stdout
    assert "Skip reason" not in result.stdout
