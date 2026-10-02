# seohub-client

`seohub-client` is the public thin client for SeoHub v2.

Installing it is not the same as being authorized to use SeoHub. Installation is intentionally available without platform authorization, but functional SeoHub source collection and analytical execution require a registered, active project on the hosted SeoHub control plane.

The current execution path is:

```
client GitHub Actions
→ short-lived GitHub OIDC
→ immutable GitHub repository identity
→ hosted SeoHub
→ registered + active project
→ provider/analytics operation
→ verified artifact delta
→ client repository commit
```

The public repository does **not** contain the autonomous SeoHub Metrika, GSC, Webmaster, Topvisor, SERP, current-state, period-analysis, traffic-summary, source-quality or hosted paid runtime implementations.

Older v1 public revisions remain in Git history. Their existence is historical; v2 changes the production execution boundary and does not rewrite published history.

## What remains public

The v2 bundle contains only what the client needs to install and communicate safely:

- bootstrap, update and rollback lifecycle;
- project/config protocol material needed for interoperability;
- GitHub OIDC acquisition and HTTPS transport;
- typed hosted-operation request building;
- bounded upload of project-owned input artifacts;
- response/hash verification;
- fail-closed atomic `write` / `delete` artifact application;
- managed GitHub Actions workflows and readiness UX;
- generic project-owned external-storage materialization needed to send existing project data.

There is no generic remote Python executor and no arbitrary `/execute` route.

## Authorization

All functional managed workflows use the hosted API. The repository variable `SEOHUB_HOSTED_BASE_URL` must point to an HTTPS SeoHub hosted endpoint.

The workflow requests a short-lived GitHub OIDC token with audience `seohub-hosted-v1`. The server authenticates the immutable GitHub repository identity and fails closed when the repository is unregistered, the identity does not match, or the project is inactive.

Deleting a client-side readiness check cannot restore autonomous execution because the proprietary implementations are absent from this bundle.

## Client-owned provider credentials (BYOK)

Provider credentials belong to the client and are supplied from GitHub Secrets only for the relevant request:

- `YANDEX_METRIKA_TOKEN`
- `YANDEX_WEBMASTER_TOKEN`
- `GOOGLE_SEARCH_CONSOLE_CREDENTIALS_JSON`
- `GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN` when that OAuth flow needs it
- `TOPVISOR_USER_ID`
- `TOPVISOR_API_KEY`
- `XMLSTOCK_USER_ID`
- `XMLSTOCK_API_KEY`

These credentials are request-scoped. The client does not persist them into project artifacts. Hosted SeoHub must not persist, log, or return them.

XMLStock remains a paid operation. Client-owned XMLStock credentials do not replace platform authorization or spend authorization: a paid call still requires an active project, the applicable paid capability, account-specific price estimation and an explicit existing spend authorization before reserve/execution/settlement.

Readiness never performs a paid XMLStock call.

## Project data ownership and transport

Normalized SEO history remains in the client repository or its already configured project-owned external storage. SeoHub does not turn PostgreSQL into an analytics warehouse.

For each hosted operation the thin client sends only the artifacts required by that operation: relevant manifests, affected monthly partitions, project configuration, normalization/section identity, semantic-core metadata and compact search state as applicable. It does not upload the whole repository.

When project history is already externalized through the existing storage-ref layer, the thin client materializes only the required logical inputs and preserves that externalized representation when applying changed normalized artifacts. The storage endpoint/bucket settings remain repository variables and storage credentials remain GitHub Secrets.

The hosted response is a deterministic artifact delta. For a `write`, the client verifies the project-relative path and content SHA-256. For a `delete`, the current file must exist and its SHA-256 must equal `previous_sha256`. The complete delta is validated before mutation; application is backed up and rolled back on a partial failure.

## Install

A new repository needs one project-owned installer workflow before Actions can run. Copy `examples/install-seohub-full.yml` to `.github/workflows/install-seohub.yml`, pin both the Action reference and `client_version` to the same accepted immutable 40-character public client SHA, commit it, and run **Install SeoHub**.

Release preparation replaces the marker `a7726b9983e2f65c62af14a00e9b070a090b2ffd` with the reviewer-accepted immutable public revision; production instructions must never use `@main` or a feature-branch ref.

The installer creates:

- `project.yaml`;
- `.seohub/client.json`;
- the managed hosted thin workflows.

Install does not call provider APIs, execute analytics, or spend money. A project can therefore be **installed but not operational**.

## Readiness

The managed readiness workflow reports separate states for:

1. client installation integrity;
2. hosted endpoint configuration;
3. GitHub OIDC/platform authorization;
4. repository registration;
5. active/inactive project status;
6. source configuration;
7. required provider Secret presence;
8. analytical data readiness;
9. paid capability readiness separately.

Provider connectivity that depends on proprietary logic is exercised only through authorized hosted operations, not by local collector code.

Before project bootstrap can be locally ready, the project owner must also make the required project-owned decisions such as `config/url-normalization.json` and `config/sections.json`.

## Managed workflows

The installed client includes hosted launchers for:

- Yandex Metrika;
- Google Search Console;
- Yandex Webmaster;
- Topvisor semantic-core inspection/candidate generation;
- current-state generation;
- explicit period analysis / traffic summary;
- readiness.

Topvisor inspection only writes a candidate. It does not automatically accept or mutate the authoritative semantic core.

The crawler is not treated as a production-working feature and is not part of the v2 hosted thin-client migration.

## Update from v1

Do not reinstall a project from scratch. Update the project-owned installer workflow to the newly accepted immutable public SHA, run `plan-update`, review the managed-file plan, then run `update`.

The lifecycle replaces old autonomous managed workflows with hosted thin workflows while preserving project-owned `project.yaml`, `config/**`, `events/**`, `research/**`, `reports/**`, and `data/**`. A locally modified managed file blocks update rather than being overwritten silently.

## Provenance

`distribution/source-manifest.json` records the private SeoHub source revision from which this public tree was deterministically exported. The private repository remains the source of truth; this public repository is an exported distribution.

No open-source license is currently published here; licensing remains a separate product/legal decision.
