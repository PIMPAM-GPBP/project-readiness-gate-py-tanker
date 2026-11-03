# Python 3.11.9 backport plan

Status: approved by the team on 3 November 2026 (with the rollback correction in step 5 applied); implementation may begin.

Target: run development, tests and Posit Connect on **CPython 3.11.9**. Reviewed on 3 November 2026 against commit `b38825b3c82e716f6e5c2409f20c6214b88f065a`, with three GPT-6.1 Sol agents checking code compatibility, dependencies and deployment validation. Revised the same day after a Claude Fable 5.1 review that ran the migration end to end on 3.11.9 (see "Dry run" below) and after the prod Connect deployment attempt of 2 November (see "Deployment evidence").

**Expected surface: seven existing files, including one application module, plus a new `.gitattributes`.** The dry run found no further changes; the checks below define when the migration is complete.

## Change surface

| File | Planned change and current evidence |
| --- | --- |
| [.python-version](../.python-version) | Replace `3.12` with `3.11.9`. rsconnect-python reads this file first when writing the manifest and turns a plain `3.11.9` into the constraint `~=3.11.0`. |
| [pyproject.toml](../pyproject.toml) | Replace `requires-python = ">=3.12"` (line 8) with `">=3.11.9"`; set Ruff to `py311` (line 37) and explicitly set mypy's `python_version = "3.11"`. An exact `==3.11.9` is **not** used: it would block developers on another 3.11 patch, and rsconnect ignores it anyway because `.python-version` takes priority. |
| [pcn_clone/ai/assess.py](../pcn_clone/ai/assess.py) | Replace `_validated[R: _Request]` (line 163) with a module-level `TypeVar("R", bound=_Request)` and an ordinary function declaration; add the typing import. Preserve the relationship between the input class and returned instance. |
| [uv.lock](../uv.lock) | Regenerate for the new Python constraint. The current lock declares `>=3.12` (line 3). The dry run relocked to the identical 58 package versions, so no dependency changes are expected; explain any that appear. |
| [requirements.txt](../requirements.txt) | Regenerate the runtime-only export from the new lock using the command recorded in its header. The dry-run export was byte-identical to the current file. |
| [manifest.json](../manifest.json) | Regenerate under 3.11.9 after bundled files are final. Expect `python.version` `3.11.9` and `environment.python.requires` `~=3.11.0`; these currently contain `3.12.15` and `~=3.12.0` (lines 9 and 18). Refresh file checksums. |
| [README.md](../README.md) | Update setup and deployment versions (lines 13 and 163) and add `-x 'tasks/**' -x 'dist/**'` to the manifest command: rsconnect does not read `.gitignore` or `.gitattributes`, so a `uv build` output directory or the task documents would otherwise be listed in the manifest. |
| `.gitattributes` (new) | Add `tasks/ export-ignore`. Task documents stay committed, so their history is reviewable, but `git archive` (which Connect uses for git-backed deployment) leaves them out of the bundle. Verified locally: a repository with this attribute archives without `tasks/`. (`dist/` is already gitignored.) |

No feature, UI, prompt, schema or stored-data changes are planned. Keep the [golden fixtures](../tests/fixtures/README.md) unchanged. No tracked CI workflow or Dockerfile exists.

## Python version constraint (decided)

Connect selects an interpreter from `environment.python.requires` in the committed `manifest.json` and fails the deployment when nothing installed satisfies it. The two servers differ at the patch level:

| Server | Installed Python | Matches `~=3.11.0` | Matches `==3.11.9` |
| --- | --- | --- | --- |
| Prod Connect (2026.08.0) | 3.8.18, 3.9.13, 3.11.9 | yes, 3.11.9 | yes |
| QA Connect | 3.14.3, 3.12.12, 3.11.14 | yes, 3.11.14 | **no, deploy fails** |

Use `~=3.11.0` (any 3.11.x), which the README procedure writes automatically. 3.11.9 remains the tested deployment target; step 1 verifies that prod actually selects it.

## Evidence collected

| Check | Result and limit |
| --- | --- |
| Compile all application and test sources with **3.11.9** | **150 of 151 files compile; one fails:** `assess.py:163`, `expected '('`. Coverage: 93 application and 58 test files. This checks syntax, not imports or execution. |
| Ruff with `--target-version py311` | One error at the same generic declaration. Python documents this syntax as a [3.12 addition](https://docs.python.org/3.12/whatsnew/3.12.html#pep-695-type-parameter-syntax). |
| Installed dependency metadata | Of 57 locked third-party packages, 56 are installed at their locked versions; none of those 56 declares a `Requires-Python` constraint excluding 3.11.9. Windows-only `colorama` is absent locally and unverified. |
| Existing offline unit suite on **3.12.15** | **1,271 passed, 124 deselected** (browser and live excluded). Baseline only. |

No incompatible standard-library API was identified in the source audit. Existing union annotations, `datetime.UTC` and `asyncio.timeout` do not need conversion for 3.11.9; the latter two were also checked on that interpreter.

### Dry run on 3.11.9 (3 November 2026, macOS arm64, scratch copy of the repo)

The source and configuration changes above were applied to a copy and every step 3 check was run under an interpreter reporting `3.11.9`:

| Check | Result |
| --- | --- |
| `uv lock` with `requires-python = ">=3.11.9"` | Resolved 58 packages; **package versions identical** to the current lock. `uv sync --locked` installed cleanly. |
| `uv export …` requirements | **Byte-identical** to the committed `requirements.txt`. |
| Compile check (script below) | 0 failures after the `TypeVar` change. |
| `ruff check .`, `ruff format --check .` | Pass (154 files). |
| `mypy pcn_clone` | Pass, strict, 93 files. |
| `uv build` | sdist and wheel built. |
| `pytest -q -m "not live"` | **1,394 passed, 1 deselected**; the 123 browser tests executed (Chrome present), none skipped. |
| README manifest command | Wrote `python.version` `3.11.9` and `requires` `~=3.11.0`. |

This resolves the earlier open question about `cp311` wheels for the locked versions on this platform; Linux installation on Connect is covered by step 4.

### Deployment evidence (prod Connect, 2 November 2026)

The team deployed the current `main` to prod Connect through **New Content from Git Repository**. Connect Session v2026.08.0 rejected it:

```
Cannot find compatible environment: no compatible Local environment with
Python version ~=3.12.0 (available versions: 3.8.18, 3.9.13, 3.11.9)
```

This confirms three things: prod reads `environment.python.requires` from the committed manifest, 3.11.9 is the only usable runtime there, and the deployment path is git-backed. For git-backed content Connect [uses `git archive` of the repository](https://docs.posit.co/connect/user/git-backed/), not the manifest's file list, so `manifest.json` and `requirements.txt` must be committed and the manifest's `-x` exclusions do not shrink the bundle (tracked `tests/` ships; harmless).

Reproduce the syntax check with an interpreter whose `--version` reports exactly 3.11.9:

```bash
python3.11 - <<'PY'
import sys
from pathlib import Path
assert sys.version_info[:3] == (3, 11, 9)
failures = 0
for root in ("pcn_clone", "tests"):
    for path in sorted(Path(root).rglob("*.py")):
        try:
            compile(path.read_text(), str(path), "exec", dont_inherit=True)
        except SyntaxError as error:
            failures += 1
            print(f"{path}:{error.lineno}: {error.msg}")
sys.exit(bool(failures))
PY
```

Other audit commands: `.venv/bin/python -m ruff check --no-cache --target-version py311 .` and `.venv/bin/python -m pytest -q -m "not browser and not live" -p no:cacheprovider`. Dependency evidence came from matching `uv.lock` against `importlib.metadata`, then evaluating each installed `Requires-Python` with `packaging.specifiers.SpecifierSet` for `3.11.9`.

## Implementation sequence and acceptance

1. **Confirm deployment prerequisites.** Prod Connect is 2026.08.0 with Python 3.8.18, 3.9.13 and 3.11.9 (from the 2 November error). Record the operating system and architecture if available. The manifest constraint is `~=3.11.0`; after deployment, verify the running process reports 3.11.9. [Posit documents manifest constraints separately from fallback version matching](https://docs.posit.co/connect/admin/python/#python-version-matching).
2. **Apply the source and configuration changes.** Create a fresh 3.11.9 environment, regenerate the lock without a blanket dependency upgrade, and confirm the diff is limited to the `requires-python` header and resolution markers. Include an isolated package build: the `hatchling` build dependency is unpinned and outside the lock.
3. **Pass the existing checks on exactly 3.11.9.** Record the interpreter version; run `uv sync --locked`, `uv build --python 3.11.9`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy pcn_clone`, and `uv run pytest -q -m "not live"`. Require the browser suite to execute with Chrome installed; its missing-Chrome skip is not a pass. Preserve fixture outputs and cover the existing Connect-like routing and WebSocket tests.
4. **Regenerate and commit deployment artifacts.** Export requirements, then generate the manifest using the [README procedure](../README.md#deploying-on-posit-connect) with the new exclusions. Check the runtime constraint (`~=3.11.0`), FastAPI entrypoint, checksums and that no `tasks/` or `dist/` entries appear. After the first deployment, confirm `tasks/` is absent from the bundle Connect received (Source Versions, or the downloaded bundle). Commit `manifest.json` and `requirements.txt`; Connect reads them from the repository. Verify the exported requirements install independently in a clean 3.11.9 environment on Linux (the deployment platform).
5. **Validate on Connect and hand over.** Deploy from the branch or tag with `PCN_AI_PROVIDER=fake`; check content and vanity URLs, assets, callbacks, project persistence and a complete review over WebSocket. Record the server's actual Python version and test results. Rollback target is the **last successfully deployed, compatible** commit or bundle on that server, not the current `main`: the current baseline requires Python 3.12 and already failed on prod, so reverting to it would not restore service. On prod no successful deployment exists yet, so a rollback there means withdrawing the content rather than reverting. Live model calls are outside the required migration checks.

## Team review

Approved on 3 November 2026 with one correction, now applied in step 5: roll back to the last successfully deployed, compatible commit or bundle, since the Python 3.12 baseline already failed on prod. The constraint decision (`>=3.11.9` for the project, `~=3.11.0` in the manifest) stands. Remaining items: the Linux installation check in step 4 and the validation in step 5.
