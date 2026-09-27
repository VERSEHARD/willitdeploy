# WillItDeploy? v0.1

A research prototype for empirically testing whether a public Node.js repository still installs/builds across multiple Node runtimes.

It intentionally starts small:

- public GitHub repositories only
- root-level Node projects only
- static dependency/runtime-risk triage
- optional real build matrix on Node 22 / 24 / 26
- SQLite scan history
- failure-signature detection for common runtime/toolchain breakage
- Bootstrap 5 UI + vanilla JavaScript (no AI-generated component framework spaghetti)

## Important security note

**Full build mode executes `npm install` and the repository's `npm run build` scripts. Those scripts are arbitrary code.**

For v0.1:

1. Keep the deployment private / obscure.
2. Set a strong `SCAN_TOKEN`.
3. Only scan repositories you trust.
4. Do not place valuable secrets in this Railway service.
5. Leave `ALLOW_FULL_BUILDS=0` for a public-facing demo.

This is an experiment runner, not a hardened multi-tenant sandbox yet.

## Railway deployment

1. Unzip this project and push it to a GitHub repository.
2. Create a new Railway project from the repository.
3. Railway will use the included `Dockerfile`.
4. Add a Railway volume mounted at `/data` so scan history and downloaded Node runtimes persist.
5. Add variables:

```text
SCAN_TOKEN=<long random string>
ALLOW_FULL_BUILDS=1
DATA_DIR=/data
SCAN_WORKERS=1
```

6. Generate a public domain in Railway.
7. Open the app. Enter the same `SCAN_TOKEN` in the UI.

If you only want static analysis, keep:

```text
ALLOW_FULL_BUILDS=0
```

## Why runtime binaries are downloaded at scan time

A single container cannot normally have three different active Node runtimes via one package manager. WillItDeploy downloads the official Linux binary tarball for the latest patch release of each selected Node major from nodejs.org and caches it under `/data/runtimes`.

For each runtime, it copies the repo into a fresh workspace, puts that Node runtime first on `PATH`, and runs:

```text
npm ci        # when package-lock.json exists
npm install   # otherwise
npm run build # only when a build script exists
```

The matrix therefore measures actual install/build behavior, rather than only reading declared engine metadata.

## First experiment

Start with **Static triage** on a few repositories.

Then enable full builds and run the bundled self-test. The first run is slower because Node 22/24/26 binaries are downloaded. Later runs reuse the cached runtimes.

After that, scan a small controlled dataset — e.g. 20 public Node repos — and look for the interesting class:

```text
Node 22 ✅
Node 24 ✅
Node 26 ❌
```

That is the seed for the actual dataset: dependencies / dependency combinations that predict future runtime breakage.

## API

### Health

```http
GET /api/health
```

### Start scan

```http
POST /api/scans
X-Scan-Token: <token>
Content-Type: application/json

{
  "repo_url": "https://github.com/owner/repo",
  "branch": null,
  "mode": "full",
  "runtimes": [22, 24, 26]
}
```

### Fetch scan

```http
GET /api/scans/<id>
```

### History

```http
GET /api/scans
```

## Known v0.1 limitations

- no monorepo workspace selection yet
- no pnpm/yarn/bun build execution yet
- no Python runtime matrix yet
- no hardened container-per-scan sandbox yet
- system library differences can cause failures unrelated to Node itself
- build scripts that require secrets/external services can fail
- npm/network flakiness can create false failures

These are deliberate. We want to test whether the underlying dataset is interesting before building infrastructure around it.
