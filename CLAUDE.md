# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`ecommerce_integrations` is a Frappe/ERPNext app (upstream: `frappe/ecommerce_integrations`) that connects ERPNext to four external ecommerce/retail platforms: **Shopify**, **Unicommerce**, **Zenoti**, and **Amazon** (via SP-API). Each integration is a self-contained sub-package under `ecommerce_integrations/` with its own settings doctype, sync logic, and tests. Requires Frappe/ERPNext >= 16.0.0, < 17.0.0 and Python >= 3.10.

PRs go to the `develop` branch only (enforced by a pre-commit hook: `no-commit-to-branch: develop` blocks committing directly to `develop` locally).

## Bench Commands

Run from the bench root (e.g. `/home/erp/frappe-bench16/`), not from the app directory.

```bash
# Install on a site
bench --site <site> install-app ecommerce_integrations

# Run all tests for this app
bench --site <site> run-tests --app ecommerce_integrations

# Run tests for one integration module
bench --site <site> run-tests --module ecommerce_integrations.shopify.tests.test_product
bench --site <site> run-tests --module ecommerce_integrations.unicommerce.tests.test_order

# Run a single test case/method
bench --site <site> run-tests --module ecommerce_integrations.shopify.tests.test_product --test TestProduct

# Migrate after doctype/patch changes
bench --site <site> migrate
```

`before_tests` (`ecommerce_integrations/utils/before_test.py`) runs the ERPNext setup wizard for a test company ("Wind Power LLC", India) if no company exists yet — required scaffolding for the test suite.

## Linting & Formatting

Pre-commit is configured (`.pre-commit-config.yaml`): ruff (import sort + lint + format, line length 110, tabs, double quotes, Python 3.10 target — see `pyproject.toml`), prettier, and eslint for JS/Vue/SCSS.

```bash
cd apps/ecommerce_integrations
pre-commit install
pre-commit run --all-files
```

Note: `.flake8` also exists but with nearly everything ignored — ruff (via `pyproject.toml`) is the actual enforced linter.

## Architecture

### Shared core (`ecommerce_integrations/controllers/`, `ecommerce_integrations/utils/`)

These are the glue every integration builds on:

- **`Ecommerce Item`** (`ecommerce_integrations/doctype/ecommerce_item/`) — the cross-integration mapping table linking an ERPNext Item (`erpnext_item_code`) to a platform-specific product (`integration`, `integration_item_code`, `variant_id`, `sku`). All "is this already synced?" and "find the ERPNext item for this platform SKU" logic goes through helper functions here (`is_synced`, `get_erpnext_item`, `get_erpnext_item_code`, `create_ecommerce_item`) rather than querying `Item` directly.
- **`Ecommerce Integration Log`** (`ecommerce_integrations/doctype/ecommerce_integration_log/`) — shared structured logging/error-tracking doctype used by every integration's sync/webhook code via `create_log(...)`. Auto-cleared after 120 days (`default_log_clearing_doctypes` in hooks.py). Each integration has its own thin wrapper (e.g. `shopify/utils.py:create_shopify_log`) that calls into this.
- **`controllers/setting.py`** — `SettingController` is the abstract base every integration's settings doctype controller extends (e.g. `is_enabled()`, warehouse mapping getters). Look here before assuming a settings doctype's contract.
- **`controllers/inventory.py`** — shared query for finding items whose stock (`Bin`) has changed since last sync (`inventory_synced_on` on `Ecommerce Item`), used by inventory-push jobs across integrations.
- **`controllers/customer.py`** — `EcommerceCustomer` base class for platform-specific customer sync.
- **`controllers/scheduling.py`** — `need_to_run()`: a debounce helper so a `cron`-scheduled job only actually executes once its per-setting configurable interval has elapsed (interval + last-run timestamp stored as fields on the settings singleton).
- **`utils/taxation.py`, `utils/price_list.py`, `utils/naming_series.py`** — cross-cutting helpers wired into core ERPNext doctypes via `doc_events` in hooks.py (e.g. tax template validation on `Item`, discarding stale `Item Price` entries).

### Integration modules differ in shape — read the right pattern before adding code

- **Shopify** (`shopify/`) is class/object-oriented: `ShopifyProduct`, `ShopifyOrder`, etc. wrap a platform entity and its sync behavior. API calls are made through the `shopify` Python SDK, gated by the `@temp_shopify_session` decorator (`shopify/connection.py`), which opens a scoped SDK session (supports both static-token and OAuth2 client-credentials auth, auto-refreshing tokens) and is a no-op during tests (`frappe.flags.in_test`).
- **Unicommerce** (`unicommerce/`) is function/module-oriented: plain functions per concern (`product.py`, `order.py`, `grn.py`, `invoice.py`, `inventory.py`, ...) that take an explicit `UnicommerceAPIClient` (`unicommerce/api_client.py`) instance rather than a decorator-managed session.
- **Amazon** (`amazon/`) and **Zenoti** (`zenoti/`) are the smallest integrations — mostly settings doctypes plus a handful of module-level sync functions (see `scheduler_events` in hooks.py for their entry points).

When adding a new sync path to an existing integration, match that integration's existing pattern rather than the other's.

### Sync entry points are declared in `hooks.py`, not discovered from code

`hooks.py` is the map of *when* integration code runs:
- `doc_events` — hooks into core ERPNext doctypes (`Item`, `Sales Order`, `Stock Entry`, `Item Price`, `Pick List`, `Sales Invoice`) for real-time sync-on-save/submit behavior (e.g. Unicommerce GRN upload on Stock Entry submit, Shopify item upload on Item insert/update).
- `scheduler_events` — polling/push jobs, including a `cron` block running every 5 minutes for Unicommerce order/inventory sync. Note the `_long` suffixed queues (`daily_long`, `hourly_long`) used for jobs expected to take a while (Zenoti sync, Unicommerce bulk operations) — use the same convention for new long-running jobs.
- `doctype_js` — client scripts injected into standard doctypes (Shopify Setting, Sales Order/Invoice, Item, Stock Entry, Pick List) live under `public/js/<integration>/`, plus `public/js/common/ecommerce_transactions.js` shared across integrations.

Real-time webhooks (e.g. Shopify) are handled separately from `doc_events`/`scheduler_events` — see `shopify/doctype/shopify_webhooks/` and the webhook registration logic invoked from `Shopify Setting`.

### Testing conventions

- Tests use `frappe.tests.IntegrationTestCase`, subclassed per-integration (`shopify/tests/utils.py:TestCase`, `unicommerce/tests/utils.py`) to set up a working settings doc in `setUpClass`.
- Shopify tests fake HTTP responses via `pyactiveresource.testing.http_fake` — `self.fake(endpoint, body=..., method=...)` registers a canned response, and fixture JSON payloads live in `shopify/tests/data/`. Use `self.load_fixture(name)` to load one directly.
- Unicommerce tests have their own `fixtures/` directory and `test_client.py` for mocking `UnicommerceAPIClient` calls.
- When adding sync logic that calls out to a platform API, add a corresponding fixture + `self.fake(...)` (Shopify) or client-level mock (Unicommerce) rather than hitting the real API in tests.
