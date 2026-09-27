# WillItDeploy? v0.2.2

Research prototype for empirically testing Node.js runtime compatibility.

## What v0.2.2 adds

- Replaced the hand-rolled frontend shell with pinned **Tabler Core 1.6.0** (MIT), a human-maintained dashboard UI package.
- Fixed optional branch/project-path parsing so JSON `null` can never become the literal path `None`.

- Finds Node projects below repo root instead of treating non-root projects as failures.
- Detects Node pins from `engines.node`, Volta, `.nvmrc`, `.node-version`, `.tool-versions`, Docker `FROM node:...`, and Railpack/Nixpacks config.
- Parses transitive native/runtime-sensitive packages from npm lockfiles.
- For npm projects without a lockfile, queries npm package metadata recursively **without executing package scripts** to look for risky transitive dependencies.
- Full builds install devDependencies correctly (v0.1 incorrectly set `NODE_ENV=production`).
- Clean Node 22/24/26 install/build matrix with npm/node versions and failure signatures.
- Regression fixtures for the two bugs discovered during v0.1 testing: transitive `deasync` and Volta Node pins.
- Optional project path for monorepos.

- **Ephemeral build storage:** downloaded Node runtimes, tarballs, npm caches, cloned repos, and build workspaces now live under `/tmp/willitdeploy` instead of the Railway volume. Only the small SQLite scan history remains in `DATA_DIR`.

## Railway deployment

Deploy with the included Dockerfile. A Railway volume at `/data` is optional and is used only to persist the small SQLite scan history. Large Node runtimes and build workspaces use ephemeral `/tmp` storage and are recreated after a fresh deployment.

Recommended variables:

```env
DATA_DIR=/data
TEMP_STORAGE_DIR=/tmp/willitdeploy
SCAN_WORKERS=1
SCAN_TOKEN=choose-a-long-private-token
ALLOW_FULL_BUILDS=1
MAX_REPO_MB=250
INSTALL_TIMEOUT_SECONDS=480
BUILD_TIMEOUT_SECONDS=480
STATIC_REGISTRY_MAX_PACKAGES=160
STATIC_REGISTRY_MAX_DEPTH=4
```

After changing `ALLOW_FULL_BUILDS`, redeploy the service. If your old v0.2 deployment filled `/data/runtimes`, you may delete that old folder after upgrading; v0.2.1 no longer reads it.

## First experiment sequence

1. Run **bundled self-test**. All regression checks and Node 22/24/26 fixture builds should pass.
2. Static scan `axept/prejss`, branch `master`. v0.2 should surface transitive `deasync` even though the repo has no npm lockfile.
3. Static scan `trailheadapps/visualforce-to-lwc`, branch `main`. v0.2 should detect the Volta Node `20.15.0` pin.
4. Full-build `axept/prejss` across Node 22/24/26. Record the matrix; do not infer anything from static score alone.

## Safety

Full-build mode executes the target repository's npm install/build scripts. It is a research prototype, not a hardened sandbox. Keep the instance private and only full-build repositories you deliberately trust. Static mode does not execute package lifecycle scripts.
