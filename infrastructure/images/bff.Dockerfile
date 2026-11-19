# Node and the Linux arm64 pnpm artifact are pinned in tools/toolchain.json.
FROM docker.io/library/node:24.22.3-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54 AS node
FROM node AS workspace

RUN node --input-type=module -e '\
    import { createHash } from "node:crypto"; \
    import { writeFile } from "node:fs/promises"; \
    if (process.arch !== "arm64") throw Error("Build the BFF for linux/arm64"); \
    const response = await fetch("https://github.com/pnpm/pnpm/releases/download/v12.18.0/pnpm-linux-arm64.tar.gz"); \
    if (!response.ok) throw Error("pnpm download failed (" + response.status + ")"); \
    const artifact = Buffer.from(await response.arrayBuffer()); \
    if (createHash("sha256").update(artifact).digest("hex") !== "700bd1aa4028465b80e62b7c5bec8455be60bbafaf53a3262a4ab8e2057712ac") throw Error("pnpm checksum mismatch"); \
    await writeFile("/tmp/pnpm.tar.gz", artifact);' \
    && mkdir -p /usr/local/lib/pnpm \
    && tar -xzf /tmp/pnpm.tar.gz -C /usr/local/lib/pnpm \
    && ln -s /usr/local/lib/pnpm/pnpm /usr/local/bin/pnpm \
    && rm /tmp/pnpm.tar.gz

WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/
COPY packages/client-ts ./packages/client-ts
ENV CI=true NEXT_TELEMETRY_DISABLED=1

FROM workspace AS build
RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --store-dir=/pnpm/store --frozen-lockfile --ignore-scripts
COPY apps/web ./apps/web
WORKDIR /app/apps/web
RUN test -f next.config.mjs && test -f instrumentation.ts && test -f app/health/ready/route.ts \
    && node node_modules/next/dist/bin/next build --webpack \
    && rm -rf .next/cache
RUN mkdir /runtime && cp package.json next.config.mjs /runtime/ \
    && if [ -d public ]; then cp -R public /runtime/; fi

FROM workspace AS production-dependencies
RUN --mount=type=cache,target=/pnpm/store \
    pnpm --filter @opentour/web... install --store-dir=/pnpm/store \
      --prod --frozen-lockfile --ignore-scripts

FROM node

COPY --from=production-dependencies /app/node_modules /app/node_modules
COPY --from=production-dependencies /app/apps/web/node_modules /app/apps/web/node_modules
COPY --from=production-dependencies /app/packages/client-ts /app/packages/client-ts
WORKDIR /app/apps/web
COPY --from=build /runtime/ ./
COPY --from=build /app/apps/web/.next ./.next
# Use the same empty Fargate volume seed as the control API image.
RUN install -d -m 0755 -o 0 -g 0 /run/opentour \
    && install -d -m 0700 -o 10001 -g 10001 /run/opentour/runtime
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
USER 10001:10001
VOLUME ["/run/opentour"]
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/health/ready', {redirect: 'error', signal: AbortSignal.timeout(2000)}).then(response => {process.exitCode = Number(response.status !== 200)}, () => {process.exitCode = 1})"]
CMD ["node", "node_modules/next/dist/bin/next", "start", "--hostname", "0.0.0.0", "--port", "3000"]
