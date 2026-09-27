# WillItDeploy? v0.3.0

Research prototype for empirically separating **Node runtime** compatibility failures from **npm/package-manager** compatibility failures.

## v0.3.0

- Runs a controlled Node × npm matrix instead of changing Node and npm at the same time.
- Full scans currently test Node 22, 24 and 26 against pinned npm 10.9.9 and npm 11.19.0.
- Downloads exact npm CLI releases once into ephemeral temp storage and reuses them across Node runtimes.
- Automatically classifies the result as an npm split, Node split, Node×npm interaction, baseline failure, clean matrix, or network-inconclusive.
- Keeps transitive native-addon detection, runtime pin detection, failure signatures and monorepo support.
- Build logs now open in a fixed modal instead of expanding the result grid and breaking the page layout.
- UI remains pinned to the human-maintained Tabler Core 1.6.0 package.

## Railway variables

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

## Next controlled experiment

Run:

- Repository: `Crypto-Mikael/tailwind-material`
- Branch: `main`
- Project path: `.`
- Node: 22, 24, 26
- Mode: Full Node × npm matrix

The scanner will run six combinations automatically:

```text
Node 22 + npm 10.9.9
Node 22 + npm 11.19.0
Node 24 + npm 10.9.9
Node 24 + npm 11.19.0
Node 26 + npm 10.9.9
Node 26 + npm 11.19.0
```

This specifically tests whether the earlier Node-22-pass / Node-24-and-26-fail result was actually caused by npm 10 vs npm 11 lockfile validation.

## Safety

Full-build mode executes the target repository's install/build scripts. Processes are privilege-dropped and receive a sanitized environment, but this is **not a hardened hostile-code sandbox**. Keep the instance private and full-build repositories you deliberately trust.
