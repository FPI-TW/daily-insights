FROM node:22-alpine AS build

WORKDIR /workspace

RUN corepack enable && corepack prepare pnpm@11.15.1 --activate

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/package.json
COPY packages/api-client/package.json packages/api-client/package.json
RUN pnpm install --frozen-lockfile --filter @daily-insights/web...

COPY apps/web apps/web
COPY packages/api-client packages/api-client
RUN pnpm --filter @daily-insights/web build

FROM node:22-alpine AS runtime

ENV NODE_ENV=production
WORKDIR /app

COPY --from=build /workspace/apps/web/.output ./.output

USER node
EXPOSE 3000

CMD ["node", ".output/server/index.mjs"]
