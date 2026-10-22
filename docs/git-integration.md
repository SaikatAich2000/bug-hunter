<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg"><img alt="Bug Hunter" src="assets/logo-light.svg" width="64"></picture></p>

# Git integration

Create and remove feature branches for User Stories.

> [← Back to the README](../README.md)

## Git integration (Story feature branches)

Bug Hunter supports exactly two optional remote Git operations, both disabled by
default:

1. create one feature branch for one **User Story** in one GitHub repository;
2. remove exactly that tracked branch again.

Pull requests, merges, tags, pipeline sync and bulk deletion are out of scope.
Bug Hunter never runs local `git`; every provider call is REST.

**Project settings → Git integration** holds the API base URL, organization,
default base branch and an encrypted project token (Fernet, via
`GIT_CREDENTIAL_ENCRYPTION_KEY`), and has **Test connection** and read-only
repository discovery (at most 200 repositories). There's no repository
allow-list: the repository is chosen when a branch is created.

**Creating.** `POST /api/git/work-items/{id}/branches` accepts only
`provider_repo_id` and `base_branch`. The name is deterministic:
`feature_<story-number>_<title-slug>` (slug from the first 25 characters of the
title). The server verifies the repository and base branch, reads the base SHA,
creates the branch, reconciles an uncertain success, and audits the outcome.
Repeated requests are idempotent. `GET .../branches/preview` validates the same
way without creating anything. Other item types are refused with
`Feature branches can be created only for User Stories.`

**Removing.** `DELETE /api/git/branches/{branch_record_id}` needs
`GIT_BRANCH_DELETION_ENABLED=true`. Any member of the Story's project may remove
the branch, and anyone outside the project gets a 404. The server deletes only
the exact `refs/heads/<stored-name>` of an `Active` record with a `feature_`
prefix, and it refuses protected names (`main`, `master`, `dev`, `develop`,
`release`) and the project's base branch. A provider 404 counts as already gone.
Auth, TLS, timeout, rate-limit and 5xx failures leave the record `Active` so the
removal can be retried. Removed branches stay visible as history.

**Credentials.** The project token takes precedence over the deployment-wide
`GITHUB_TOKEN`. A token is never accepted from API payloads, returned in a
response, or written to logs or audit rows. Use a fine-grained PAT with
`Metadata: Read` and `Contents: Read and Write`, and set `GIT_CA_BUNDLE_FILE`
behind a TLS-intercepting proxy.
