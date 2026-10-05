# ChatGPT project operations guide

This document is the operating map for ChatGPT when working inside an SeoHub client project.

It describes **how to discover capabilities, choose evidence, and select tools**. It is a routing guide, not a closed list of allowed scenarios. If a valid project capability is not named here, use the same principles: discover it, verify its semantics, choose the shortest reliable path, and preserve evidence boundaries.

## 1. Start by discovering the project

Do not assume every SeoHub project has the same sources, history depth, workflows, storage layout, or optional providers.

For a substantive task, discover only what is relevant. Useful project entry points include:

- `project.yaml` — project identity, timezone, enabled/configured source families;
- `.seohub/client.json` — installed client identity and managed workflow set;
- `data/aggregates/current-state.json` — preferred compact analytical entry point when present;
- `reports/` — reporting rules and durable project reports;
- `config/` — project-owned methodology, URL normalization, sections, search profiles, semantic core;
- `events/` — project/site/methodology timeline;
- `research/` — project-specific evidence that is not a primary production metric;
- `.github/workflows/` — executable project capabilities;
- `data/normalized/`, manifests, and storage references — detailed persisted evidence;
- `tasks/`, strategy, audit, or other project-owned directories when the request is about those areas.

The absence of a path is information about project capability, not proof that the underlying real-world phenomenon is absent.

## 2. Route by evidence need, not by a fixed question list

First identify what kind of evidence the request requires. Typical evidence needs include, but are not limited to:

- current project state;
- historical change;
- traffic and landing-page behaviour;
- search visibility, clicks, impressions, CTR, or positions;
- query/page/section localization;
- indexing or diagnostics;
- technical crawl evidence;
- SERP/competitor evidence;
- demand/seasonality;
- site/project change history;
- implementation/configuration state;
- external search-market context;
- a reproducible report or decision record.

A single request may need several evidence types. Keep each source's role explicit rather than forcing everything into one metric.

## 3. Prefer the smallest sufficient evidence layer

Use the cheapest reliable layer that can answer the next uncertainty:

1. compact current state;
2. compact deterministic summary;
3. period/source-specific derived analysis;
4. relevant manifest/event/config evidence;
5. the smallest normalized partition needed;
6. raw/provider evidence only when necessary.

Do not scan full history merely because it exists.

Escalate to a deeper layer when the current layer cannot answer a concrete question, not as a ritual.

## 4. Source roles are capabilities, not interchangeable metrics

When present and compatible, common source roles are:

- **Yandex Metrika** — onsite organic traffic, landing pages, sections, engine/device contribution and behaviour;
- **Google Search Console** — Google search clicks, impressions, CTR, position, page/query trends;
- **Yandex Webmaster** — Yandex search-query evidence, indexing, pages in search, diagnostics and targeted URL evidence;
- **rank/SERP measurements** — fixed-universe visibility, ranking URLs, SERP composition and competitor evidence for an explicit profile;
- **Topvisor** — optional client/reference measurement series when the project has stored/configured Topvisor evidence; when live API access is available through the approved project/hosted path, prefer the official `api.topvisor.net` mirror as the default transport and treat `api.topvisor.com` as an explicit fallback, not a distinct methodology;
- **crawler evidence** — technical site-state evidence when an authoritative compatible crawl exists;
- **events** — factual project/site/methodology timeline;
- **external web research** — external context such as search updates, demand/news, competitors or SERP context when project evidence alone cannot answer the question.

These roles are examples, not an exhaustive catalog. New or project-specific sources should be interpreted from their contracts/configuration before use.

Do not substitute an incompatible source merely because the preferred source is missing.

## 5. Discover executable capabilities before inventing a path

If fresh data or an operation may be needed, inspect the project's existing workflows/actions/configuration first.

Prefer an existing project capability over creating a new script or manual workaround.

For a source or operation, determine:

- is it configured/enabled for this project;
- is there already a workflow/action or hosted path;
- is the path read-only or write-producing;
- what period/profile/filters it supports;
- whether it incurs variable cost;
- what artifact it writes or returns;
- how freshness and failure are represented.

Do not infer that a source is callable merely because its API exists somewhere in SeoHub.

## 6. Tool selection: shortest reliable path

Choose the path with the fewest unnecessary hops and side effects.

### GitHub repository content

For repository files, commits, diffs, branches, pull requests, Actions metadata, and other GitHub-native state:

1. prefer the direct ChatGPT GitHub connector/API when it supports the task;
2. use repository files already available in the ChatGPT Project when they are sufficient;
3. use a remote shell/clone only when execution, local-only state, or an unsupported GitHub operation genuinely requires it.

Do not clone a repository on a server just to read files that the GitHub connector can read directly.

### Code execution and tests

Use an execution environment when the task genuinely requires running code, tests, or reproducing runtime behaviour.

Execution is not a substitute for direct repository access. Read authoritative GitHub state through GitHub; use compute to execute/validate it.

### External/current information

Use web research when the request depends on current external facts or when external context is a plausible explanation that project evidence cannot establish.

Do not begin with broad external research before determining what actually changed in the project, unless the user's question is itself external.

### Provider/API access

Use an already approved direct connector, API path, project workflow, or hosted runtime according to the project's configured capability.

Prefer read-only access when the question only needs observation.

## 7. Freshness and refresh decisions

Freshness is relative to the question and source semantics.

Before refreshing anything:

1. identify the exact source/evidence that is insufficient;
2. determine whether stored data already covers the requested period;
3. determine whether the source's normal publication lag explains the apparent staleness;
4. identify the smallest refresh that would resolve the uncertainty.

If a refresh writes data, commits files, changes repository state, or triggers another durable mutation, it requires explicit operator authorization.

If a refresh is paid or may incur variable cost, it also requires the applicable spend authorization.

If a safe read-only check exists and does not mutate project state, it may be used when materially useful.

Do not refresh every source just to make timestamps uniform.

## 8. Failure and missing-data semantics

Preserve distinct states:

- source unavailable;
- auth/config failure;
- API/provider error;
- partial/truncated/sampled/privacy-limited data;
- source-limited universe;
- pending or delayed finality;
- genuinely empty result;
- not measured/not enabled.

Never convert these states into zero.

A missing source does not prove no traffic, no visibility, no query, no page, no technical issue, or no event.

## 9. Compatibility before comparison

Before comparing values, verify the dimensions that matter for that source, such as:

- dates and period length;
- timezone;
- filters/segments;
- property/host/counter identity;
- engine/region/device/language;
- provider/profile/mode;
- keyword universe;
- normalization/taxonomy methodology;
- finality or source-quality state.

Do not silently stitch incompatible series.

## 10. General investigation pattern

For analytical questions, a useful default sequence is:

1. **fitness** — can the available evidence answer the question;
2. **detect** — what changed;
3. **localize** — where it changed;
4. **decompose** — which source/dimension explains the movement mathematically;
5. **timeline** — what relevant project/site changes overlap;
6. **alternatives** — what other explanations remain plausible;
7. **conclusion** — facts, associations, hypotheses, recommendations.

This is a default reasoning structure, not a restriction. Skip irrelevant steps and add domain-specific checks when the question requires them.

## 11. Example routing patterns

These examples illustrate the principles; they do not define the only supported requests.

| Need | Useful first evidence | Possible deeper evidence |
| --- | --- | --- |
| overall project status | current state | compact summaries, relevant source detail |
| traffic change | traffic summary / Metrika-derived state | landing/section/device partitions |
| Google query movement | GSC comparable query dynamics | relevant GSC normalized partitions |
| Yandex query movement | Webmaster comparable checkpoints/dynamics | targeted exact-period or URL evidence |
| fixed-core visibility | current rank/SERP summary | compatible rank history / SERP evidence |
| client/reference positions | exact-date stored Topvisor evidence | compatible prior Topvisor measurement |
| indexing issue | Webmaster current/indexing evidence | URL-level checks, sitemap/config/events |
| technical issue | authoritative crawler summary if available | relevant crawl partitions / targeted evidence |
| suspected release impact | current-state + project events | Git diff / affected pages / source movement |
| seasonality or algorithm hypothesis | project movement first | external search/demand/web research |
| implementation question | project config/workflows | shared docs only if operator explicitly requests another repo |

When a new scenario does not fit this table, infer the evidence type and apply Sections 1–10.

## 12. Large/externalized data

A project may store large datasets outside Git and keep references/manifests in the repository.

When a validated storage reference exists:

- use the reference as the authoritative locator;
- fetch only the object/partition required for the current question;
- preserve its lineage, methodology, and coverage;
- do not treat an unavailable object-store request as an empty dataset.

Do not read an entire external history when a compact summary or one partition is sufficient.

## 13. Reports and durable artifacts

Reports should derive from the same deterministic evidence used for analysis.

When present:

- `reports/WEEKLY_REPORT.md` defines the normal weekly view;
- `reports/MANAGER_WEEKLY_REPORT.md` defines the short manager-facing view.

Other report types may exist or be added. Reuse their project-specific rules rather than forcing every request into the weekly format.

Creating or updating a durable report file is a Git write and follows the project's write-authorization rule.

## 14. When the available tooling is unclear

Do not guess.

Inspect the smallest set of project-owned discovery files needed to answer:

- which sources are configured;
- what data already exists;
- what workflows/actions are available;
- what storage mechanism is in use;
- what analytical contracts/reports the project declares.

If a required capability still cannot be found, state that it is unavailable or undiscovered in the current project boundary.

Do not leave the repository to search for hidden implementation details unless the operator explicitly asks.

## 15. Principle of operation

The goal is not to use every tool.

The goal is to answer the operator's request with the **minimum sufficient, freshest compatible, reproducible evidence**, using the **most direct available path**, while avoiding unnecessary writes, cost, infrastructure, and context switching.
