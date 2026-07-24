<!-- intent-skills:start -->

# TanStack Intent - before editing files, run the matching guidance command.

tanstackIntent:

- id: "@tanstack/devtools#devtools-app-setup"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/devtools#devtools-app-setup"
  for: "Install TanStack Devtools, pick framework adapter (React/Vue/Solid/Preact), register plugins via plugins prop, configure shell (position, hotkeys, theme, hideUntilHover, requireUrlFlag, eventBusConfig). TanStackDevtools component, defaultOpen, localStorage persistence."
- id: "@tanstack/devtools#devtools-marketplace"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/devtools#devtools-marketplace"
  for: "Publish plugin to npm and submit to TanStack Devtools Marketplace. PluginMetadata registry format, plugin-registry.ts, pluginImport (importName, type), requires (packageName, minVersion), framework tagging, multi-framework submissions, featured plugins."
- id: "@tanstack/devtools#devtools-plugin-panel"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/devtools#devtools-plugin-panel"
  for: "Build devtools panel components that display emitted event data. Listen via EventClient.on(), handle theme (light/dark), use @tanstack/devtools-ui components. Plugin registration (name, render, id, defaultOpen), lifecycle (mount, activate, destroy), max 3 active plugins. Two paths: Solid.js core with devtools-ui for multi-framework support, or framework-specific panels."
- id: "@tanstack/devtools#devtools-production"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/devtools#devtools-production"
  for: "Handle devtools in production vs development. removeDevtoolsOnBuild, devDependency vs regular dependency, conditional imports, NoOp plugin variants for tree-shaking, non-Vite production exclusion patterns."
- id: "@tanstack/devtools-event-client#devtools-bidirectional"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/devtools-event-client#devtools-bidirectional"
  for: "Two-way event patterns between devtools panel and application. App-to-devtools observation, devtools-to-app commands, time-travel debugging with snapshots and revert. structuredClone for snapshot safety, distinct event suffixes for observation vs commands, serializable payloads only."
- id: "@tanstack/devtools-event-client#devtools-event-client"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/devtools-event-client#devtools-event-client"
  for: "Create typed EventClient for a library. Define event maps with typed payloads, pluginId auto-prepend namespacing, emit()/on()/onAll()/onAllPluginEvents() API. Connection lifecycle (5 retries, 300ms), event queuing, enabled/disabled state, SSR fallbacks, singleton pattern. Unique pluginId requirement to avoid event collisions."
- id: "@tanstack/devtools-event-client#devtools-instrumentation"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/devtools-event-client#devtools-instrumentation"
  for: "Analyze library codebase for critical architecture and debugging points, add strategic event emissions. Identify middleware boundaries, state transitions, lifecycle hooks. Consolidate events (1 not 15), debounce high-frequency updates, DRY shared payload fields, guard emit() for production. Transparent server/client event bridging."
- id: "@tanstack/devtools-vite#devtools-vite-plugin"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/devtools-vite#devtools-vite-plugin"
  for: "Configure @tanstack/devtools-vite for source inspection (data-tsd-source, inspectHotkey, ignore patterns), console piping (client-to-server, server-to-client, levels), enhanced logging, server event bus (port, host, HTTPS), production stripping (removeDevtoolsOnBuild), editor integration (launch-editor, custom editor.open). Must be FIRST plugin in Vite config. Vite ^6 || ^7 only."
- id: "@tanstack/react-start#lifecycle/migrate-from-nextjs"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/react-start#lifecycle/migrate-from-nextjs"
  for: "Step-by-step migration from Next.js App Router to TanStack Start: route definition conversion, API mapping, server function conversion from Server Actions, middleware conversion, data fetching pattern changes."
- id: "@tanstack/react-start#react-start"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/react-start#react-start"
  for: "React bindings for TanStack Start: createStart, StartClient, StartServer, React-specific imports, re-exports from @tanstack/react-router, full project setup with React, useServerFn hook."
- id: "@tanstack/react-start#react-start/server-components"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/react-start#react-start/server-components"
  for: "Implement, review, debug, and refactor TanStack Start React Server Components in React 19 apps. Use when tasks mention @tanstack/react-start/rsc, renderServerComponent, createCompositeComponent, CompositeComponent, renderToReadableStream, createFromReadableStream, createFromFetch, Composite Components, React Flight streams, loader or query owned RSC caching, router.invalidate, structuralSharing: false, selective SSR, stale names like renderRsc or .validator, or migration from Next App Router RSC patterns. Do not use for generic SSR or non-TanStack RSC frameworks except brief comparison."
- id: "@tanstack/router-core#router-core"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/router-core#router-core"
  for: "Framework-agnostic core concepts for TanStack Router: route trees, createRouter, createRoute, createRootRoute, createRootRouteWithContext, addChildren, Register type declaration, route matching, route sorting, file naming conventions. Entry point for all router skills."
- id: "@tanstack/router-core#router-core/auth-and-guards"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/router-core#router-core/auth-and-guards"
  for: "Route protection with beforeLoad, redirect()/throw redirect(), isRedirect helper, authenticated layout routes (_authenticated), non-redirect auth (inline login), RBAC with roles and permissions, auth provider integration (Auth0, Clerk, Supabase), router context for auth state."
- id: "@tanstack/router-core#router-core/code-splitting"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/router-core#router-core/code-splitting"
  for: "Automatic code splitting (autoCodeSplitting), .lazy.tsx convention, createLazyFileRoute, createLazyRoute, lazyRouteComponent, getRouteApi for typed hooks in split files, codeSplitGroupings per-route override, splitBehavior programmatic config, critical vs non-critical properties."
- id: "@tanstack/router-core#router-core/data-loading"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/router-core#router-core/data-loading"
  for: "Route loader option, loaderDeps for cache keys, staleTime/gcTime/ defaultPreloadStaleTime SWR caching, pendingComponent/pendingMs/ pendingMinMs, errorComponent/onError/onCatch, beforeLoad, router context and createRootRouteWithContext DI pattern, router.invalidate, Await component, deferred data loading with unawaited promises."
- id: "@tanstack/router-core#router-core/navigation"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/router-core#router-core/navigation"
  for: "Link component, useNavigate, Navigate component, router.navigate, ToOptions/NavigateOptions/LinkOptions, from/to relative navigation, activeOptions/activeProps, preloading (intent/viewport/render), preloadDelay, navigation blocking (useBlocker, Block), createLink, linkOptions helper, scroll restoration, MatchRoute."
- id: "@tanstack/router-core#router-core/not-found-and-errors"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/router-core#router-core/not-found-and-errors"
  for: "notFound() function, notFoundComponent, defaultNotFoundComponent, notFoundMode (fuzzy/root), errorComponent, CatchBoundary, CatchNotFound, isNotFound, NotFoundRoute (deprecated), route masking (mask option, createRouteMask, unmaskOnReload)."
- id: "@tanstack/router-core#router-core/path-params"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/router-core#router-core/path-params"
  for: "Dynamic path segments ($paramName), splat routes ($ / _splat), optional params ({-$paramName}), prefix/suffix patterns ({$param}.ext), useParams, params.parse/stringify, pathParamsAllowedCharacters, i18n locale patterns."
- id: "@tanstack/router-core#router-core/search-params"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/router-core#router-core/search-params"
  for: "validateSearch, search param validation with Zod/Valibot/ArkType adapters, fallback(), search middlewares (retainSearchParams, stripSearchParams), custom serialization (parseSearch, stringifySearch), search param inheritance, loaderDeps for cache keys, reading and writing search params."
- id: "@tanstack/router-core#router-core/ssr"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/router-core#router-core/ssr"
  for: "Non-streaming and streaming SSR, RouterClient/RouterServer, renderRouterToString/renderRouterToStream, createRequestHandler, defaultRenderHandler/defaultStreamHandler, HeadContent/Scripts components, head route option (meta/links/styles/scripts), ScriptOnce, automatic loader dehydration/hydration, memory history on server, data serialization, document head management."
- id: "@tanstack/router-core#router-core/type-safety"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/router-core#router-core/type-safety"
  for: "Full type inference philosophy (never cast, never annotate inferred values), Register module declaration, from narrowing on hooks and Link, strict:false for shared components, getRouteApi for code-split typed access, addChildren with object syntax for TS perf, LinkProps and ValidateLinkOptions type utilities, as const satisfies pattern."
- id: "@tanstack/router-plugin#router-plugin"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/router-plugin#router-plugin"
  for: "TanStack Router bundler plugin for route generation and automatic code splitting. Supports Vite, Webpack, Rspack, and esbuild. Configures autoCodeSplitting, routesDirectory, target framework, and code split groupings."
- id: "@tanstack/start-client-core#start-core"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/start-client-core#start-core"
  for: "Core overview for TanStack Start: tanstackStart() Vite plugin, getRouter() factory, root route document shell (HeadContent, Scripts, Outlet), client/server entry points, routeTree.gen.ts, tsconfig configuration. Entry point for all Start skills."
- id: "@tanstack/start-client-core#start-core/auth-server-primitives"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/start-client-core#start-core/auth-server-primitives"
  for: "Server-side authentication primitives for TanStack Start: session cookies (HttpOnly, Secure, SameSite, __Host- prefix), session read/issue/destroy via createServerFn and middleware, OAuth authorization-code flow with state and PKCE, password-reset enumeration defense, CSRF for non-GET RPCs, rate limiting auth endpoints, session rotation on privilege change. Pairs with router-core/auth-and-guards for the routing side."
- id: "@tanstack/start-client-core#start-core/deployment"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/start-client-core#start-core/deployment"
  for: "Deploy to Cloudflare Workers, Netlify, Vercel, Node.js/Docker, Bun, Railway. Selective SSR (ssr option per route), SPA mode, static prerendering, ISR with Cache-Control headers, SEO and head management."
- id: "@tanstack/start-client-core#start-core/execution-model"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/start-client-core#start-core/execution-model"
  for: "Isomorphic-by-default principle, environment boundary functions (createServerFn, createServerOnlyFn, createClientOnlyFn, createIsomorphicFn), ClientOnly component, useHydrated hook, import protection, dead code elimination, environment variable safety (VITE_ prefix, process.env)."
- id: "@tanstack/start-client-core#start-core/middleware"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/start-client-core#start-core/middleware"
  for: "createMiddleware, request middleware (.server only), server function middleware (.client + .server), context passing via next({ context }), sendContext for client-server transfer, global middleware via createStart in src/start.ts, middleware factories, method order enforcement, fetch override precedence."
- id: "@tanstack/start-client-core#start-core/server-functions"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/start-client-core#start-core/server-functions"
  for: "createServerFn (GET/POST), validator (Zod or function), useServerFn hook, server context utilities (getRequest, getRequestHeader, setResponseHeader, setResponseStatus), error handling (throw errors, redirect, notFound), streaming, FormData handling, file organization (.functions.ts, .server.ts)."
- id: "@tanstack/start-client-core#start-core/server-routes"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/start-client-core#start-core/server-routes"
  for: "Server-side API endpoints using the server property on createFileRoute, HTTP method handlers (GET, POST, PUT, DELETE), createHandlers for per-handler middleware, handler context (request, params, context), request body parsing, response helpers, file naming for API routes."
- id: "@tanstack/start-server-core#start-server-core"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/start-server-core#start-server-core"
  for: "Server-side runtime for TanStack Start: createStartHandler, request/response utilities (getRequest, setResponseHeader, setCookie, getCookie, useSession), three-phase request handling, AsyncLocalStorage context."
- id: "@tanstack/virtual-file-routes#virtual-file-routes"
  run: "pnpm dlx @tanstack/intent@latest load @tanstack/virtual-file-routes#virtual-file-routes"
  for: "Programmatic route tree building as an alternative to filesystem conventions: rootRoute, index, route, layout, physical, defineVirtualSubtreeConfig. Use with TanStack Router plugin's virtualRouteConfig option."

<!-- intent-skills:end -->

### Git rules

1. Never use --no-verify
2. Branch names must use `<type>/<summary-kebab-case>`, for example `feat/platform-command-split` or `fix/auth-refresh-bug`.
3. Commit messages must use `<type>: <summary>`, for example `feat: split package scripts by platform` or `fix: guard renderer process access`.
4. Recommended commit types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `build`, `ci`.
5. Unless the user asks, never relax TypeScript, lint, or format constraints, and never skip tests.

### Validation Rules and Checklist

1. Formatting
   - Prompt: Apply and verify formatting rule.
   - When to use: Before committing or after broad edits, normalize formatting across the codebase.
   - Checklist:
     - Run `format`.
     - Run `format:check`.
     - Confirm only intended files were changed.

2. Git Hook Maintenance
   - Prompt: Apply and verify git hook rule.
   - When to use: When pre-commit and pre-push checks must stay enforced and consistent.
   - Checklist:
     - Ensure hooks are executable.
     - Ensure hooks include `format:check` and `lint:check`.
     - If type-checking or tests are enabled, ensure those checks are included.

3. Lint Validation
   - Prompt: Run and validate lint rule.
   - When to use: After adding rules or changing tooling, verify lint consistency.
   - Checklist:
     - Run `lint`.
     - Run `lint:check`.
     - Fix max-warnings and remaining lint violations.

4. Test Configuration
   - Prompt: Validate test runner setup and execution.
   - When to use: When test rules are added or test configuration changes.
   - Current test runner: `vitest`
   - Checklist:
     - Confirm the selected test runner matches the project setup.
     - Run `test`.
     - Run `test:coverage` when coverage validation is needed.

5. TypeScript Settings
   - Prompt: Validate TypeScript strictness and config.
   - When to use: When tsconfig or strict typing rules are changed.
   - Checklist:
     - Run `type:check`.
     - Confirm strict compiler options remain enabled.
     - Ensure `exclude` does not hide product-logic paths.

### Package Rules and Checklist

1. Zod
   - Prompt: Use Zod at trust boundaries and keep inferred types close to schemas.
   - When to use: When validating request data, environment values, form payloads, or API responses.
   - Checklist:
     - Define schemas before consuming untrusted values.
     - Use `safeParse` for recoverable validation paths.
     - Export inferred types only when they reduce duplicate interface definitions.

2. Tailwind CSS
   - Prompt: Use Tailwind utilities consistently and extract repeated UI patterns into components.
   - When to use: When styling TanStack Start routes, layouts, or shared UI primitives.
   - Checklist:
     - Keep class lists readable and grouped by layout, spacing, color, and state.
     - Use a shared class-name helper when composing conditional class names.
     - Avoid duplicating long class combinations across components.

3. TanStack Query
   - Prompt: Use TanStack Query for client-side server state and keep cache keys stable.
   - When to use: When caching, mutating, or invalidating remote data beyond route loaders.
   - Checklist:
     - Use structured query keys that include every data dependency.
     - Prefer route loaders and server functions for route-level data dependencies.
     - Keep mutation invalidation scoped to affected queries.

4. TanStack Router
   - Prompt: Use TanStack Router route definitions as the source of truth for navigation and data loading.
   - When to use: When adding routes, route params, search params, loaders, or navigation.
   - Checklist:
     - Keep route params and search params typed through router APIs.
     - Place route-level data dependencies in loaders when appropriate.
     - Do not edit generated route tree files manually.

5. TanStack Form
   - Prompt: Use TanStack Form for typed form state, validation, and submission workflows.
   - When to use: When building non-trivial forms in TanStack Start routes or components.
   - Checklist:
     - Keep default values and field names fully typed.
     - Define validation at the form or field boundary closest to the constraint.
     - Submit sensitive mutations through server functions.

6. TanStack Store
   - Prompt: Use TanStack Store for shared client-owned state with granular subscriptions.
   - When to use: When UI state must be shared beyond a local component or route boundary.
   - Checklist:
     - Keep server data in loaders or TanStack Query instead of duplicating it in a store.
     - Subscribe only to the state slice a component needs.
     - Keep store actions close to the state transitions they implement.

7. TanStack Table
   - Prompt: Use TanStack Table for typed, headless table state and rendering.
   - When to use: When building sortable, filterable, paginated, or selectable data tables.
   - Checklist:
     - Keep column definitions typed against the row model.
     - Use controlled state when table filters or pagination belong in the URL.
     - Keep server-side pagination and sorting aligned with loader or query inputs.

8. i18next + react-i18next
   - Prompt: Use react-i18next translation keys instead of hard-coded user-facing strings.
   - When to use: When adding localized text or formatting to TanStack Start routes and components.
   - Checklist:
     - Keep translation keys stable and descriptive.
     - Use interpolation instead of string concatenation.
     - Keep locale files organized by route or feature.
