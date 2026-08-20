from scripts.check_conventional_commits import validate_message


def test_valid_dependabot_message_keeps_generated_summary_lines() -> None:
    message = """\
chore(actions): bump the github-actions group with 3 updates

Bumps the github-actions group with 3 updates: [actions/checkout](https://github.com/actions/checkout), [astral-sh/setup-uv](https://github.com/astral-sh/setup-uv) and [pypa/gh-action-pypi-publish](https://github.com/pypa/gh-action-pypi-publish).

Updates `actions/checkout` from 7.0.0 to 7.0.1
- [Release notes](https://github.com/actions/checkout/releases)
- [Changelog](https://github.com/actions/checkout/blob/main/CHANGELOG.md)
- [Commits](https://github.com/actions/checkout/compare/v7...3d3c42e5aac5ba805825da76410c181273ba90b1)

---
updated-dependencies:
- dependency-name: actions/checkout
  dependency-version: 7.0.1
  dependency-type: direct:production
  update-type: version-update:semver-patch
  dependency-group: github-actions
- dependency-name: astral-sh/setup-uv
  dependency-version: 10.0.1
  dependency-type: direct:production
  update-type: version-update:semver-major
  dependency-group: github-actions
- dependency-name: pypa/gh-action-pypi-publish
  dependency-version: 1.14.2
  dependency-type: direct:production
  update-type: version-update:semver-patch
  dependency-group: github-actions
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

    assert validate_message(message, label="Dependabot commit") == []
