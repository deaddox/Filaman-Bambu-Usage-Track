# Public Release Plan for v3.0.0

This is a detailed plan that can be executed by an AI coding agent or a human maintainer to prepare the repo for a public GitHub release.

## Goal

Publish a clean, public v1 release of the Bambu Lab FilaMan plugin without exposing credentials, internal session data, or local developer artifacts.

## Release Checklist

### 1) Pre-flight security sweep

- Search the repo for common secret indicators:
  - private key blocks
  - `.env` files
  - `client_secret`, `access_key`, `api_key`, `token`, `password`
  - browser capture artifacts such as `.har`
- Delete or purge any captured browser sessions, local debug exports, or screenshots that include authentication data.
- Remove compiled Python artifacts:
  - `__pycache__/`
  - `*.pyc`
- Confirm there are no tracked secret files in git history or current working tree.
- Confirm `.gitignore` includes the sensitive patterns before publishing.

### 2) Repo hygiene

- Ensure the repo contains only project source, docs, tests, and public release metadata.
- Remove large local data files or generated models unless they are intentionally part of the public distribution.
- Confirm release-specific docs are included: setup guide, user docs, security notes, and release plan.
- Ensure there is a clean working tree before tag and release creation.

### 3) Validate project health

- Run the full unit test suite.
- Confirm the project passes with no failures.
- Check that release metadata is valid:
- semver is proper (`3.0.0`)
- plugin manifest version matches release tag version
- plugin key and driver key are consistent and distinct from the built-in Bambu driver

### 4) Prepare public-facing documentation

- Update the project README with:
  - feature summary
  - quick install instructions
  - link to the user setup guide
  - public GitHub release readiness note
- Add a user setup guide covering:
  - prerequisites
  - installation steps
  - required config values
  - verification steps
  - troubleshooting
  - security notes
- Add a maintainer guide if necessary for plugin framework compatibility.

### 5) Package release artifact correctly

- Build the plugin ZIP from the source directory root.
- Ensure the ZIP contains only the plugin files at the archive root:
  - `plugin.json`
  - `__init__.py`
  - `driver.py`
- Keep the plugin directory layout flat at package root.
- Exclude `__pycache__`, compiled files, and generated artifacts.
- Verify the release ZIP is valid for FilaMan install/upgrade rules.

### 6) Create and verify GitHub release

- Create a GitHub repository release tagged as `v3.0.0`.
- Attach the cleaned plugin ZIP asset.
- Add public release notes with:
  - summary of features
  - compatibility notes
  - installation instructions
  - limitations or known issues
- Confirm the release page does not include internal tokens, URLs, or private logs.

### 7) Repository final pass

- Check `git status` is clean except intended release changes.
- Review the diff for accidental files and private content.
- Verify no large session artifacts or debug files remain.
- Make sure the public repo is clearly labeled as the open-source version.

## Executable Agent Tasks

### Task A: Security scan

1. Search for common sensitive patterns across the repo.
2. Delete or git-rm any files matching:
   - `*.har`
   - `.env*`
   - `*.pem`, `*.key`, `*.crt`, `*.p12`, `*.pfx`
   - `__pycache__/`
   - `*.pyc`
3. Ensure `.gitignore` blocks these patterns in future.
4. Validate there are no tracked secret files left.

### Task B: Release metadata update

1. Confirm plugin manifest version is `3.0.0`.
2. Confirm README and release docs align with the public-facing version.
3. Ensure the public docs mention the repo is intended for public GitHub use.

### Task C: Documentation pass

1. Create or update the user setup guide.
2. Add emergency or troubleshooting guidance for connection/setup problems.
3. Document security best practices and artifact cleanup requirements.

### Task D: Validation

1. Run the automated tests.
2. Check that all passed tests remain green.
3. Build the plugin ZIP using the release-safe packaging rules.
4. Validate ZIP structure and allowed file types.

### Task E: GitHub publication

1. Create or update the public GitHub repo settings if needed.
2. Push the cleaned branch.
3. Create a release tag `v3.0.0`.
4. Upload the release ZIP asset.
5. Publish the release notes.

## Acceptance Criteria

The release is considered ready when all of the following are true:

- No secrets or private data remain in the repo.
- The project passes its automated tests.
- The plugin version is set to `3.0.0`.
- User-facing setup documentation is complete.
- The release ZIP is valid and correctly structured.
- The GitHub repo can be made public without exposing private artifacts.

## Suggested Commit Message

`chore: prepare public v3.0.0 release and sanitize repo`
