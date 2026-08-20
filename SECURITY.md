# Security policy

## Supported versions

Security fixes are released for the latest published `pydantic-versions`
version. Earlier releases should be upgraded before reporting an issue that is
already fixed in the current release.

The `main` branch and unreleased commits are development code. They are not
supported release targets, but security fixes normally land there first.

## Reporting a vulnerability

Use **Security > Report a vulnerability** in this GitHub repository to open a
private security advisory. If private reporting is unavailable, contact the
maintainer through GitHub and request a private channel without including
vulnerability details in the initial public message.

Do not open a public issue or pull request with exploit details, credentials,
private data, or other sensitive information. Include the affected package,
Python, and Pydantic versions, a minimal synthetic reproduction when needed,
the expected impact, and any known mitigation.

## Dependency security maintenance

Dependabot monitors Python and GitHub Actions dependencies. CI also runs the
locked development environment through `pip-audit`; a reported vulnerability
must be triaged before a release candidate is accepted. A dependency may keep
the lowest compatible version floor when the vulnerable range is excluded by
the lockfile and supported endpoint tests, but unresolved runtime or build
chain vulnerabilities must be fixed or explicitly risk-accepted before
release.
