ARG NODE_IMAGE
FROM ${NODE_IMAGE}
ARG PNPM_URL
ARG PNPM_SHA256
RUN PNPM_URL="${PNPM_URL}" PNPM_SHA256="${PNPM_SHA256}" node --input-type=module -e 'import{writeFile}from"node:fs/promises";import{createHash}from"node:crypto";const r=await fetch(process.env.PNPM_URL);if(!r.ok)throw Error(String(r.status));const b=Buffer.from(await r.arrayBuffer());if(createHash("sha256").update(b).digest("hex")!==process.env.PNPM_SHA256)throw Error("pnpm checksum mismatch");await writeFile("/tmp/pnpm.tar.gz",b)'
RUN mkdir -p /usr/local/lib/pnpm && tar -xzf /tmp/pnpm.tar.gz -C /usr/local/lib/pnpm && ln -s /usr/local/lib/pnpm/pnpm /usr/local/bin/pnpm && rm /tmp/pnpm.tar.gz
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/bootstrap-smoke.mjs ./apps/web/
COPY packages/client-ts ./packages/client-ts
ENV NEXT_TELEMETRY_DISABLED=1 CI=true
RUN pnpm install --frozen-lockfile --ignore-scripts
USER 10001:10001
WORKDIR /app/apps/web
CMD ["node", "bootstrap-smoke.mjs"]
