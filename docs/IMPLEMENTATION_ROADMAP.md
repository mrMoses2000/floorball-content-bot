# Floorball content platform implementation roadmap

Last updated: 2026-08-31

This document is the durable execution plan for the Telegram content bot and
`/home/moses/floorball.kz`. It is intentionally stored in the bot repository because the bot
owns editorial workflow, approval state and publication orchestration.

## Goal

Build one approval-gated content platform where:

- PostgreSQL is the private source of truth for users, conversations, consent, canonical city
  data, news and audit history;
- `floorball.kz` remains a static Vite application deployed from Git/Plesk;
- city and news content is exported into deterministic, public-only JSON bundles;
- a public Telegram deep link can create a restricted new-city application without granting
  access to editorial data;
- every visually relevant publication is built and rendered in an isolated worktree;
- Telegram receives deterministic desktop/mobile screenshots of the exact revision;
- only an expiring, actor-bound callback for that revision can cause commit and atomic push;
- contact requests are durably accepted and delivered to `Knff@gmail.com`, with retry and
  operator visibility;
- Codex extracts structured data but never decides RBAC, completeness, consent, approval or
  publication and never receives production push credentials.

## Non-negotiable boundaries

1. Private phone numbers, Telegram IDs, messages, transcripts, consent evidence and original
   media never enter Git or public bundles.
2. Content changes and code/template changes use different allowlists and approval paths.
3. Routine city/news publication does not ask an LLM to edit JSX, JSON or SQL.
4. Browser screenshots in the production workflow use a pinned Playwright/Chromium process,
   not an interactive desktop Chrome session.
5. A failed test, build, screenshot, remote-ref verification or mail-delivery check must remain
   a visible failed state; it must not be reported as success.
6. Text such as "добро" is not authorization. Only a one-use callback bound to actor, revision,
   preview hash and expiry is authorization.
7. PostgreSQL migrations and integration tests use only a disposable database whose name ends
   in an approved test suffix. Fixtures may truncate their database.

## Test-driven execution rule

For each behavior:

1. Write a behavioral test against a public contract or observable boundary.
2. Run it and record the expected failure reason.
3. Implement the smallest production behavior that satisfies the contract.
4. Run the focused test.
5. Run the relevant package suite and static checks.
6. Run cross-repository contract/E2E checks when the change crosses the DB/site boundary.
7. Update the evidence log below.

Tests must not assert implementation trivia solely to make the current implementation pass.
Prefer state transitions, persisted rows, exported contracts, authorization results, rendered
UI behavior and external-boundary fakes.

## Target data and publication flow

```text
site CTA -> Telegram start payload -> application/dialogue -> immutable draft revisions
    -> reviewer decision -> deterministic canonical projector -> canonical PostgreSQL rows
    -> public allowlist exporter -> city/federation/news JSON
    -> isolated worktree -> tests/build -> Playwright screenshots
    -> Telegram media group + approve/change/cancel callbacks
    -> atomic push main + plesk-static -> manual Plesk deployment
```

## Phase P0: complete the existing editorial workflow

Release objective: an approved trainer dialogue can be deterministically and idempotently
applied to canonical city tables; reviewers can inspect, request changes or approve without
using raw SQL.

- [x] Define the approved-draft projector contract and typed dry-run result.
- [x] Reject non-approved drafts, stale revisions, changed spec/context hashes and unauthorized
      actors.
- [x] Map allowlisted trainer fields to `cities`, `city_content`, `clubs`,
      `training_schedules`, estimates and consent-aware contacts.
- [x] Treat new-city selection as an application/provisional-city path, never implicit activation.
- [x] Make application idempotent by `(draft_id, revision)` and record before/after hashes.
- [ ] Add reviewer list/show/approve/request-changes Telegram callbacks.
- [ ] Make a requested change create or resume a new immutable revision and invalidate old
      approval callbacks.
- [ ] Add CLI dry-run/show commands for operational recovery.
- [ ] Add PostgreSQL concurrency, authorization, rollback and idempotency tests.
- [ ] Add health/reporting for submitted or blocked drafts.

Gate: no news or new-city publication work proceeds to production until the canonical projector
and reviewer path pass PostgreSQL E2E.

## Phase P1: real contact delivery

Release objective: public contact data displays `Knff@gmail.com`, and a submitted contact request
is durably recorded before asynchronous delivery.

- [ ] Replace every public `info@floorball.kz` value and `mailto:` with `Knff@gmail.com`.
- [ ] Remove the fake `preventDefault + alert(success)` implementation.
- [ ] Define a versioned contact request contract: request ID, locale, name, reply-to, subject,
      message, honeypot and anti-abuse proof.
- [ ] Add a dedicated contact relay (initial target: Google Apps Script + private Sheet outbox)
      with a fixed recipient, input limits, header-injection protection and plain-text mail.
- [ ] Add idempotency, bounded retry, sent/dead state and Telegram operator notification.
- [ ] Make frontend copy say "Заявка принята в обработку", never "Письмо доставлено".
- [ ] Add frontend validation/loading/error tests and relay contract tests.
- [ ] Perform a live smoke with a unique marker and verify both durable row and Gmail inbox.

Gate: changing `mailto:` alone is not completion. The live delivery marker must be observed in
`Knff@gmail.com`.

## Phase P2: national and city news

Release objective: approved national and city-scoped news appears on the homepage, news archive,
article route and relevant city page.

- [ ] Add canonical `news_items`, localized content, city links, sources and media links.
- [ ] Add a versioned `news` dialogue specification and deterministic gap evaluator.
- [ ] Enforce city-scope RBAC: city coaches can edit only linked cities; federation editors can
      create national news; reviewers approve; superadmins publish.
- [ ] Require RU/KZ title, excerpt and safe body blocks; require media rights/alt text only when
      media is present; treat EN/gallery/video as optional.
- [ ] Add `project_news_payload` and deterministic `news-content.json` generation.
- [ ] Extend the publisher content allowlist without allowing JSX/config/package changes.
- [ ] Replace i18n placeholder news with `NewsDataContext` data.
- [ ] Add accessible non-autoplay CSS scroll-snap carousel after Geography on the homepage.
- [ ] Add `/news`, `/news/:slug` and city-scoped news sections.
- [ ] Add contract, localization, filtering, XSS, accessibility and route tests.

## Phase P3: public new-city application

Release objective: `/start new_city` allows an unknown person to submit only their own restricted
application; a superadmin can verify it and initialize a provisional city.

- [ ] Persist Telegram start intent before contact binding.
- [ ] Add applicant/application tables separate from normal users and editor roles.
- [ ] Verify shared contact belongs to the Telegram sender; normalize phone; rate-limit attempts.
- [ ] Restrict applicants to their own request and expose no city/editor context.
- [ ] Add `city_proposal.v1` questions grouped as required-to-start, required-for-submit,
      required-for-publish and optional.
- [ ] Add duplicate-name/slug detection and advisory-lock slug reservation.
- [ ] Add idempotent `city-initialize --dry-run/--apply`.
- [ ] Create an inactive city and city content; grant `city_coach` and exactly one city scope only
      after explicit superadmin verification.
- [ ] Refactor map region metadata to be data-driven; coordinates may be absent but then the city
      must still appear in the directory.
- [ ] Replace the site CTA with `https://t.me/floorball_site_agent_bot?start=new_city`.
- [ ] Add applicant isolation, replay, duplicate, rate-limit and full staging E2E tests.

## Phase P4: visual preview and revision feedback

Release objective: every visual publication produces reproducible images of the exact approved
revision and supports an invalidate-and-rebuild change loop.

- [ ] Add `publication_artifacts` with route, language, viewport, path, SHA-256, dimensions,
      revision hash, manifest hash and retention timestamp.
- [ ] Add Telegram media-group outbox support with idempotent retry.
- [ ] Run a local preview on an allocated loopback port; block non-local network requests.
- [ ] Pin desktop `1280x720` and mobile `390x844`, timezone, locale and reduced motion.
- [ ] Derive affected routes from the changed entity.
- [ ] Fail preview if an expected screenshot is missing, corrupt, blank or has a mismatched hash.
- [ ] Bind approval to actor + publication + revision hash + screenshot manifest hash + expiry.
- [ ] Add Approve / Needs changes / Cancel callbacks.
- [ ] Invalidate every old callback and artifact manifest when revision N+1 is created.
- [ ] Add screenshot, stale-button, media-group retry and cleanup tests.

## Phase P5: crash-safe publisher

Release objective: a worker crash during Git/network activity can be reconciled without duplicate
publication or an ambiguous success message.

- [ ] Replace the long external-operation transaction with states:
      `preview_ready -> confirming -> pushing -> remote_verified -> published`.
- [ ] Use short transactions and a renewable lease around state ownership.
- [ ] Add a reconciler that compares expected commit IDs with remote `main` and `plesk-static`.
- [ ] Keep atomic push of both refs and reject base-commit drift.
- [ ] Separate content and code/template change manifests and allowlists.
- [ ] Add crash-before-push, crash-after-push, ref-mismatch and concurrent-confirm tests.

## Phase P6: staging and release

- [ ] Create a separate staging Telegram bot and PostgreSQL database.
- [ ] Use a local bare Git remote and `PUBLISH_ENABLED=false` for the first E2E.
- [ ] Test synthetic trainer flow, national news, city news and duplicate new-city application.
- [ ] Verify screenshots and stale approvals without touching production refs.
- [ ] Pilot one real national news item.
- [ ] Pilot one real city news item.
- [ ] Pilot one real new city.
- [ ] Run clean-clone install/test/lint/build, backup and restore drill.
- [ ] Manually deploy verified `plesk-static` in Plesk and smoke-test direct route reloads and
      RU/KZ/EN.

## Cross-cutting release gates

- [ ] `pytest -m 'not postgres'`, full disposable-PostgreSQL suite and Ruff are green.
- [ ] Site `npm ci`, Vitest, ESLint and production build are green from a clean checkout.
- [ ] DB-to-JSON-to-frontend round trips do not expose private fields.
- [ ] RBAC/IDOR and applicant isolation tests are green.
- [ ] Media consent withdrawal removes the public derivative on the next projection.
- [ ] Contact live delivery is verified.
- [ ] Both desktop/mobile screenshot sets exist for visual changes.
- [ ] Old callbacks fail after revision or base commit changes.
- [ ] Remote main/static commit IDs are verified before Telegram reports success.
- [ ] Backup/restore drill succeeds.

## Execution evidence

Record focused red/green outcomes here. Do not replace raw test output; keep concise references.

### 2026-08-31 baseline

- Architecture audit: complete. Chosen path is incremental PostgreSQL -> public JSON -> Git.
- Browser audit: live `/`, `/contacts` and `/clubs` inspected through Chrome.
- Bot runtime: user bot and worker active; health returned `ok: true` on 2026-08-28.
- Authorization: `+77073645718` is bound and has active `superadmin`.
- Bot unit baseline: `54 passed, 26 deselected`.
- Site unit baseline: `24 passed`.
- Both repositories were clean after the audit.

### 2026-08-31 P0 iteration 1: consent semantics and trainer dry-run contract

- RED: `test_required_boolean_consents_must_be_affirmative_not_merely_answered` failed because
  `False` was treated as a completed consent value.
- GREEN: required boolean fields with `privacy=consent` now require exactly `True`.
- RED: trainer projection suite initially failed at collection because no deterministic
  projection module existed.
- GREEN: added a non-mutating trainer projection plan with an explicit city directory,
  existing-city resolution, new-city application blocker, required-consent blockers,
  language-specific text patches, metrics, club-private-contact redaction and schedule consent.
- Focused projection tests: `4 passed`.
- Bot non-PostgreSQL regression: `59 passed, 26 deselected`.
- Ruff: all source and test checks passed.

### 2026-08-31 P0 iteration 2: approved draft -> canonical PostgreSQL

- RED: the PostgreSQL projector suite initially failed at collection because the approved-draft
  application boundary did not exist.
- GREEN: added migration `010_canonical_projection.sql` and an atomic trainer application
  service. It verifies role, approved/current revision, content hash, mode, definition version,
  definition hash, context hash, completeness and selected-city identity before any write.
- The application updates only allowlisted city/content/estimate fields and draft-owned club and
  schedule rows. Private club contacts remain private unless their own publication consent is
  affirmative.
- RED: concurrent applications exposed a PostgreSQL `SerializationError` caused by a stale
  serializable snapshot.
- GREEN: the draft row now serializes workers under READ COMMITTED, so exactly one worker applies
  and all waiters return the same persisted application result.
- RED: the normal reviewer transition reached `approved` without pinning `approved_revision`.
- GREEN: `transition_draft` now pins the current revision on approval and clears the pin when
  changes, rejection or revocation invalidates it.
- Focused PostgreSQL projector tests: `8 passed` (approval, stale pins, authorization, rollback,
  private contact, mapping, sequential idempotency and concurrent idempotency).
- Full bot suite against `floorball_bot_test`: `93 passed`.
- Ruff, compileall and dependency integrity: passed.
