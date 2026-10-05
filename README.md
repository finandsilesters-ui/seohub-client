# seohub-client

`seohub-client` is the public, distributable thin client for SeoHub. It contains only the lifecycle code, client-side collectors/analysis required by managed Actions, and the GitHub OIDC request Action.

The private/server SeoHub implementation is not included. In particular this repository does not contain hosted runtime code, PostgreSQL schema/migrations, XMLStock paid execution, provider credentials, Hostkey deployment internals, database URLs, or production Secrets.

## Install in a new private repository

A new repository needs one project-owned workflow file before GitHub Actions can run anything. Copy `examples/install-seohub-full.yml` to `.github/workflows/install-seohub.yml`, commit it, then open **Actions → Install SeoHub → Run workflow**.

The example is prepared for the deployment smoke repository `alexfchkjob-stack/seo-test` and uses:

- project id: `seo-test`
- project name: `SeoHub Test`
- domain placeholder: `example.com`
- timezone: `Etc/UTC`
- Metrika: enabled, counter `"0"`, attribution `last`
- Webmaster: enabled, host `https:example.com:443`
- Google Search Console: enabled, property `https://example.com/`
- Topvisor: enabled, project `"0"`
- XMLStock: enabled as a hosted paid-action source

The installer is pinned to public client commit `6d806b140e911ec3f7f8a5464a921aee67ce734b`. Both the Action `uses:` reference and `client_version` must be the same immutable 40-character SHA.

The install creates project-owned `project.yaml`, `PROJECT_INSTRUCTIONS.md`, and `PROJECT_OPERATIONS.md`, plus `.seohub/client.json` and the managed workflows. The instruction file is ready to paste into a ChatGPT Project; the operations file is its flexible map for discovering sources, evidence layers, workflows, storage, and tool paths. Lifecycle updates never overwrite either project-owned file. Installation does not call any source API, does not execute paid XMLStock, and does not require access to the private `finandsilesters-ui/SeoHub` repository.

## Secrets and repository variable

With every source enabled, installation reports this checklist. Values are never printed:

- `YANDEX_METRIKA_TOKEN`
- `YANDEX_WEBMASTER_TOKEN`
- `GOOGLE_SEARCH_CONSOLE_CREDENTIALS_JSON`
- `GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN` — conditional, only for OAuth-client credentials
- `TOPVISOR_USER_ID`
- `TOPVISOR_API_KEY`

Repository variable:

- `SEOHUB_HOSTED_BASE_URL`

XMLStock provider credentials are intentionally not project-side Secrets. Paid XMLStock execution is hosted; the hosted service owns `SEOHUB_XMLSTOCK_*` provider credentials. Project readiness marks XMLStock as a paid action intentionally not tested and never performs a paid provider call.

The placeholder source identifiers above prove deployment shape only. They do not imply that real API connectivity will pass before real credentials and source identifiers are configured.

## Mandatory project-owned decisions

Bootstrap does not invent business decisions. Before project readiness can become ready, add:

- `config/url-normalization.json` — see `examples/config/url-normalization.json`; `"rules": []` is an explicit decision.
- `config/sections.json` — for this deployment-only placeholder project use `examples/config/sections.not-needed.json`.

These files are project-owned and are never created or overwritten by lifecycle updates.

## Readiness versus analytical readiness

The managed readiness workflow separates:

1. client installation integrity;
2. source configuration/connectivity;
3. mandatory project bootstrap decisions;
4. hosted GitHub OIDC identity.

Analytical readiness is separate. A project is not analytically ready merely because installation and credentials are valid; compatible collected data, source-quality checks, and deterministic analysis still have to exist.

Readiness never runs paid XMLStock and never runs the crawler.

## Ownership

SeoHub-managed:

- `.seohub/client.json`
- the workflows listed by `templates/client/managed-files.json`

Project-owned and preserved by updates:

- `project.yaml`
- `PROJECT_INSTRUCTIONS.md`
- `PROJECT_OPERATIONS.md`
- `config/**`
- `events/**`
- `research/**`
- `reports/**`
- `data/**` is generated data and is not replaced by the client lifecycle
- the starter/install workflow itself

If a managed file was edited locally, update fails closed instead of overwriting it.

## Update

The install workflow has a `workflow_dispatch` operation choice. To update:

1. change the two pinned SHA occurrences in the project-owned install workflow to the new public `seohub-client` commit;
2. commit that one workflow change;
3. run **Install SeoHub** with operation `plan-update` to inspect the managed-file plan;
4. run it again with operation `update`.

Only managed files and `.seohub/client.json` change. Project-owned configuration/data stays untouched. A locally edited managed file blocks the update.

## Rollback

Git is the rollback mechanism. Reverting the client update commit restores the previous managed files and `.seohub/client.json`, including the previous immutable client SHA.

## Provenance

`distribution/source-manifest.json` records the private SeoHub source revision from which this bundle was deterministically exported. Private source and public client commit SHAs are intentionally different identities.

No open-source license is currently published here; licensing is a separate product/legal follow-up.
