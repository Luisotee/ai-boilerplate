# Docker images and process runtime

Detail behind the Docker and logging rules in the root `CLAUDE.md` / `AGENTS.md`. Read it before changing a `Dockerfile`, a `*.dockerignore` file or the volumes in `docker-compose.yml`.

## Non-root images

- The three TS clients are multi-stage builds: `tsc` → `dist/` in a builder stage, production-only deps in the runtime stage, `USER node` (uid 1000), started with `node --import ./dist/instrument.js dist/main.js` so Sentry's ESM hooks register before `main.js`'s imports are linked (`main.js` still imports `./instrument.js`; it resolves to the same module and is not evaluated twice). **Type errors fail the image build**, so never add `|| true` to the build step.
- The ai-api image runs as `appuser` (uid/gid 1000). Code and venv are root-owned/read-only, and the writable paths are `/app`, `/app/knowledge_base` and `/home/appuser/.cache` (`HF_HOME`, `EASYOCR_MODULE_PATH`, `TIKTOKEN_CACHE_DIR` for Docling/tiktoken models; the worker mounts the `model-cache` volume there).
- The ai-api Dockerfile always installs `ffmpeg` (used by pydub for TTS/STT). `poppler-utils`, `tesseract-ocr` and `libmagic1` are installed only when `INSTALL_DOCLING=true` (build arg), so the default LlamaParse-only image stays lean. Docker Compose forwards `${INSTALL_DOCLING}` from the shell environment as a build arg.

## Logging and shutdown (TS)

pino-pretty is used only when `NODE_ENV !== 'production'` (the Dockerfiles set `production`, so containers emit plain JSON). All three clients handle SIGTERM/SIGINT with `app.close()` in try/catch, flush Sentry and exit 0; startup failures and `unhandledRejection` still go through `shutdownWithError` (Sentry flush, exit 1).

## Build-context ignore files

The TS clients build from the repo root, so a `packages/<pkg>/.dockerignore` would be ignored. Each uses an allowlist `packages/<pkg>/Dockerfile.dockerignore` (BuildKit uses it *instead of* the root `.dockerignore`), which is why their contexts are a few hundred KB. The ai-api context is `packages/ai-api`, so its own `.dockerignore` applies. Keep the `.logfire/` exclusions in all of them.

## Upgrading an existing deployment to the non-root images

Named volumes created by the old root images keep root ownership, so the new processes can't write them (Baileys can't save its session; uploads fail). Once, after building the new images:
```bash
docker compose build
docker compose stop whatsapp api worker
docker compose run --rm --no-deps --user root --entrypoint chown whatsapp -R node:node /app/packages/whatsapp-client/auth_info_baileys
docker compose run --rm --no-deps --user root --entrypoint chown api -R appuser:appuser /app/knowledge_base
docker compose up -d
```
New volumes need nothing: Docker copies the image directory's ownership on first mount
