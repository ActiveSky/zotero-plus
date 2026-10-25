# Contributing

Thanks for taking the time to improve `zotero-plus`.

## Reporting a bug

Open an issue using the **Bug report** template and include:

- the command you ran (redact cookies, API keys and item keys you consider private)
- operating system and Zotero version
- `zotero-cli --help` works or not, plus `python --version`
- the full error output

For security issues, **do not open a public issue** — follow
[`SECURITY.md`](SECURITY.md).

## Suggesting a feature

Use the **Feature request** template. Describe the workflow you want, not just
the command name, so the behaviour can be tested end to end.

## Development

There is no build step and no third-party dependency:

```bash
python scripts/zotero_plus.py --help
python -m compileall -q scripts       # syntax check (also runs in CI)
```

Guidelines:

- Python 3.10+, standard library first. Adding a dependency needs a strong reason.
- Keep each command usable on Windows, macOS and Linux. No hard-coded home
  directories, usernames or package-manager paths.
- Never read or write secrets outside the documented locations
  (`~/.config/arxiv2zh/cookies.txt`, `--cookie`, Zotero's cookie jar).
- Anything that writes to Zotero must preview by default, require an explicit
  `--yes`/`--apply`, and back up first.
- Do not commit `state/`, `*.sqlite`, `__pycache__/` or cookies.

### Testing without touching a real library

Read-only commands can be tested against any Zotero library. For write
commands, use a disposable test library or a copy of the data directory:

```bash
ZOTERO_DATA_DIR=/path/to/copy python scripts/zotero_plus.py rename-title
```

## Pull requests

- Keep the change focused; separate refactors from behaviour changes.
- Update `SKILL.md` and both READMEs when a command or option changes.
- Add a `CHANGELOG.md` entry under `Unreleased`.
- Fill in the pull request template, including how you tested it.

## License

By contributing, you agree that your contribution is licensed under
**AGPL-3.0-or-later**, the license of this project.
