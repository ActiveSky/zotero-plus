# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub's
[Security Advisories](https://github.com/ActiveSky/zotero-plus/security/advisories/new)
or by contacting the maintainer listed in the repository profile.

Include:

- the affected command and version
- the operating system and Zotero version
- a minimal reproduction
- the impact you believe it has

You can expect an acknowledgement within a few days. Please allow time for a
fix and release before publishing details.

## Scope

Areas worth a careful look:

- cookie handling (`translate`): cookies must never be logged, written to the
  repository, or sent anywhere other than the configured service
- direct database writes (`rename-*`, `venue-apply`): path handling, backups,
  and what happens when Zotero is running
- secondary downloads (`pdf`, `arxiv-version`): PDF validation and where files
  are written
- shell invocation: arguments are passed as lists, never through a shell

## Out of scope

- the availability or terms of third-party services
- metadata inaccuracies from Semantic Scholar, OpenReview, DBLP, Crossref,
  PubMed, Unpaywall, OpenAlex or arXiv
- issues that require an already-compromised machine
