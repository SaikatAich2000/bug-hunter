# SonarQube report

**Status: PASS.** Analysed on a local SonarQube Community Build server
(Docker, `sonarqube:community`) with `sonar-scanner-cli`, on a clean copy of
the final tree (tracked and new files only), with the coverage reports
produced by the Linux CI replication of the same tree (2026-10-01).

## Quality gate `bug-hunter-gate`: PASSED

Created with `python scripts/sonar_gate.py apply`.

| Condition | Threshold | Result |
|---|---|---|
| Coverage, overall | >= 80% | 88.3% |
| Coverage, new code | >= 80% | 93.5% |
| Vulnerabilities | 0 | 0 |
| New vulnerabilities | 0 | 0 |
| Security rating, overall and new code | A | A |
| New issues | 0 | 0 |
| Duplicated lines on new code | <= 3% | 0.0% |

## Measures

| Measure | Value |
|---|---|
| Lines of code | 47,275 (Python `app/`, JavaScript `frontend/src`) |
| Bugs | 0 (reliability rating A) |
| Vulnerabilities | 0 (security rating A) |
| Security hotspots to review | 0 (security review rating A) |
| Code smells | 238 (maintainability rating A) |
| Line coverage | 90.9% |
| Branch coverage | 80.9% |
| Duplicated lines | 0.5% |

Coverage sources: backend `coverage.xml` from `pytest --cov` (93% of
`app/` in the Linux run), frontend `lcov.info` from Vitest v8. Tests,
built/vendored assets and frontend test files are excluded from coverage as
configured in `sonar-project.properties`; no production module is excluded.

## Findings handled in this pass

The first scan of the final tree reported 1 bug and 2 blocker smells; all
three are fixed and the scan was repeated:

| Rule | Where | Fix |
|---|---|---|
| javascript:S2871 (bug): `sort()` without a comparator | `BoardSettingsPanel.jsx`, working weekdays | numeric comparator |
| python:S3516 (blocker): function always returns the same value | `routes/bugs.py` `_bulk_delete`, `agile/taxonomy.py` `epic_progress_many` | restructured (the caller reports the outcome; one return) |
| python:S1192: event labels duplicated as literals | `agile/reports.py` | one constant per label |

A scan run with a coverage report older than the code was correctly refused
by SonarQube ("line out of range"); the reported numbers come from coverage
of the analysed tree.

## Remaining code smells (238, none bugs or vulnerabilities)

| Kind | Count | Assessment |
|---|---|---|
| Cognitive complexity above 15 (python:S3776, javascript:S3776) | most of the 40 critical | Long but tested functions (sprint analysis, sprint completion, migrations, Git integration, the rich-text editor). Splitting them is a refactor with regression risk and no behaviour change; left for planned maintenance. |
| `Depends(...)` as a default argument (python:S5717) | 1 | False positive: FastAPI evaluates it per request. |
| Duplicated string literals, naming, nesting, minor style | rest | Maintainability only. |

## Reproduce

```bash
# server (local)
docker run -d --name bh-sonar -p 127.0.0.1:9000:9000 sonarqube:community
python scripts/sonar_gate.py apply            # with SONAR_TOKEN (admin) and SONAR_HOST_URL set
# analysis, from a checkout with coverage.xml and frontend/coverage/lcov.info
scripts/sonar-scan.sh                          # or sonar-scan.ps1
```

CI runs the same analysis when the repository variable `SONAR_ENABLED=true`
and the secrets `SONAR_TOKEN` and `SONAR_HOST_URL` are set (HUMAN_ACTIONS 15
and 16).
