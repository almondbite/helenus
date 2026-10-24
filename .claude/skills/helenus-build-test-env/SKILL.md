---
name: helenus-build-test-env
description: Run, test, and set up the Helenus environment — global Python 3.12, NO pytest, NO venv, standalone test runners needing PYTHONPATH, .env secrets, Schwab OAuth token. Use when running tests, seeing ImportError/ModuleNotFoundError, setting up from scratch, or deciding whether a failure is environment vs code.
---

# Helenus build / test / environment

## 1. Purpose
Recreate and verify the working environment, and run tests the way this machine
actually runs them — which contradicts the README.

## 2. When to use
Running any test; `ModuleNotFoundError: helenus` or `No module named pytest`; fresh
setup; classifying a failure as environment vs data vs code.

## 3. When not to use
Interpreting what a passing test MEANS → `helenus-validation-and-qa`.
Live-session bugs → `helenus-debugging-playbook`.

## 4. Inputs required
Shell access. For live runs only: `.env` secrets + `schwab_token.json`.

## 5. Procedure

### The real environment (verified 2026-07-05)
- **Global Python 3.12.10** — no `.venv` exists. README's Setup section (venv steps) is
  stale-risk; deps are installed into the global interpreter.
- **pytest is NOT installed** (`requirements-dev.txt` exists but isn't installed).
- No CI, no linter, no type-checker configured.

### Running tests (the one command that matters)
Every suite is a standalone runner but imports `helenus.*`, so the repo root must be on
`PYTHONPATH`:
```bash
# Bash
PYTHONPATH=. python tests/test_gex.py
```
```powershell
# PowerShell
$env:PYTHONPATH='.'; python tests\test_gex.py
```
Expected output: `  ok  test_...` lines then `N passed`. Verified green 2026-07-05:
`test_gex.py` (10), `test_scan2.py` (26), `test_conviction.py` (27).
Run every suite: loop over `tests/test_*.py` the same way (there are 22).

### Quick syntax gate (used by past sessions, permission-allowlisted)
```bash
python -c "import ast; [ast.parse(open(f,encoding='utf-8').read(), f) for f in ['helenus/config.py','helenus/bot.py']]; print('AST OK')"
```

### Fresh setup from scratch
```powershell
pip install -r requirements.txt        # into global python, matching current reality
copy .env.example .env                 # fill: Discord token/channel, Schwab keys, Anthropic key
python scripts\authorize.py            # one-time interactive browser OAuth -> schwab_token.json
python main.py                         # LIVE bot posting to a real Discord channel —
                                       # owner-run only; never launch it as a "check"
```

### Failure classification
| Symptom | Class | Check |
|---|---|---|
| `ModuleNotFoundError: helenus` | env | PYTHONPATH not set to repo root |
| `No module named pytest` | env (expected) | use standalone runners |
| `No module named pandas/discord/...` | env | `pip install -r requirements.txt` |
| Token/401/OAuth errors at startup | credentials | `schwab_token.json` expired → re-run `scripts/authorize.py` |
| Assertion failure in a suite | code | real regression — do not "fix" the fixture; see `helenus-validation-and-qa` |
| Bot runs but no data | data/session | market closed? throttle widened premarket? see `helenus-debugging-playbook` |

## 6. Evidence required
"Tests pass" requires the suite name and its `N passed` line. "Environment works"
requires an actual command output, not the README.

## 7. Output artifact
Command transcript with pass counts in your response.

## 8. Common traps
- **Bad behavior this prevents:** a session spends 20 minutes creating a venv and
  installing pytest "per the README" and then reports the environment was broken.
  It wasn't — it just isn't what the README says.
- Installing pytest unprompted changes the environment contract — ask first.
- Running `python tests/test_x.py` without PYTHONPATH and concluding the code is broken.
- Running `main.py` "to test" — it's a live bot posting to a real Discord channel.

## 9. Related skills
`helenus-validation-and-qa`, `helenus-debugging-playbook`, `helenus-repo-orientation`.

## 10. Provenance and maintenance

Re-verify volatile claims with:
- `python --version; python -m pytest --version` (still global 3.12 / no pytest?)
- `PYTHONPATH=. python tests/test_gex.py`

Last generated: 2026-07-05
Known uncertainty:
- Owner may install pytest or create a venv later; the standalone runners work either way.
