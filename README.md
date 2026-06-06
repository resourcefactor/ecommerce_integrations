<div align="center">
    <img src="https://frappecloud.com/files/ERPNext%20-%20Ecommerce%20Integrations.png" height="128">
    <h2>Ecommerce Integrations for ERPNext</h2>

[![CI](https://github.com/frappe/ecommerce_integrations/actions/workflows/ci.yml/badge.svg)](https://github.com/frappe/ecommerce_integrations/actions/workflows/ci.yml)

</div>

### Currently supported integrations

- Shopify
- Unicommerce - [User Documentation](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/unicommerce_integration)
- Zenoti - [User documentation](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/zenoti_integration)
- Amazon - [User documentation](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/amazon_integration)

---

## Shopify Integration

### Setup

1. Go to **Shopify Setting** in ERPNext.
2. Enter your Shopify store URL (e.g. `yourstore.myshopify.com`).
3. Enter the **Admin API Access Token** from your Shopify app in the **Password / Access Token** field.  
   > In Shopify admin: Apps → Develop apps → your app → API credentials → Admin API access token (starts with `shpat_`). This is shown only once at creation — regenerate if lost.
4. Enter the **API Secret Key** in the Shared Secret field.
5. Enable Shopify and save. Webhooks are registered automatically.

---

### Pushing Items from ERPNext to Shopify

#### 1. Mark items for publishing

On each **Item**, check the **Publish on Website** checkbox. Only items with this checked will be pushed to Shopify. Items without it checked are never uploaded, even if the global upload setting is on.

> This field is generic — other integrations can use the same flag.

#### 2. Enable item upload in settings

In **Shopify Setting**, under *ERPNext to Shopify Sync*:

| Setting | Purpose |
|---|---|
| **Upload new ERPNext Items to Shopify** | Master switch for ERPNext → Shopify item push |
| **Update Shopify Item after updating ERPNext item** | Push changes to Shopify whenever an item is saved |
| **Sync New Items as Active** | New items land as "active" in Shopify; unchecked = "draft" |
| **Upload ERPNext Variants as Shopify Items** | Allow item variants to sync as separate Shopify products |

#### 3. Automatic sync on save

Once **Publish on Website** is checked and upload is enabled, saving the item pushes it to Shopify automatically.

#### 4. Manual bulk sync buttons

Two buttons on the **Shopify Setting** form:

- **Sync Items to Shopify** — queues all items with *Publish on Website = 1* that have not been synced yet. Use this after bulk-enabling the checkbox on many items.
- **Sync Price to Shopify** — pushes the current price list rate to Shopify for all already-synced items. Useful after updating prices in a price list.

---

### Price List Integration

Instead of maintaining a separate rate field per item, prices are driven by a standard ERPNext **Price List**.

In **Shopify Setting**, set the **Price List** field (under *ERPNext to Shopify Sync*) to the selling price list you want to use. When an item is pushed or scheduled-synced, its rate is looked up from **Item Price** for that price list.

- If no price list is set, the price field is left unchanged in Shopify.
- To update prices in bulk after changing rates, use the **Sync Price to Shopify** button.

---

### Item Field Mapping

The **Item Field Mapping** table (in *ERPNext to Shopify Sync* on Shopify Setting) controls exactly which ERPNext Item fields are sent to which Shopify fields. No code changes are needed to add or change a mapping — just edit the table.

#### Default mappings (pre-filled on first setup)

| ERPNext Item Field | Shopify Field |
|---|---|
| `item_name` | `title` |
| `description` | `body_html` |
| `item_group` | `product_type` |
| `brand` | `vendor` |

> Weight (`weight_per_unit` / `weight_uom`) is always synced automatically — it requires unit-of-measure conversion and is not in the table.

#### Adding custom mappings

Add a row with the ERPNext **fieldname** (not the label) and the Shopify destination:

**Standard product fields** — just use the Shopify field name:

| ERPNext Field | Shopify Field | Result |
|---|---|---|
| `brand` | `vendor` | Sets Shopify vendor |
| `item_name` | `tags` | Sets Shopify product tags |

**Variant fields** — prefix with `variant:`:

| ERPNext Field | Shopify Field | Result |
|---|---|---|
| `barcode` | `variant:barcode` | Sets barcode on the Shopify variant |
| `custom_compare_price` | `variant:compare_at_price` | Sets the compare-at price |

**Shopify Metafields** — prefix with `metafield:namespace.key`:

| ERPNext Field | Shopify Field | Result |
|---|---|---|
| `custom_material` | `metafield:custom.material` | Writes a `custom.material` metafield |
| `warranty_period` | `metafield:specs.warranty` | Writes a `specs.warranty` metafield |

> Metafields must be defined in **Shopify admin → Settings → Custom data → Products** before they appear in your storefront theme. They can be written without a definition, but won't be visible until defined.

#### Custom Shopify title

If you need a different title in Shopify than the ERPNext item name, add a custom field called `custom_shopify_title` to the Item doctype. When set, it takes priority over the `item_name → title` mapping row.

---

### Scheduled Sync

The integration runs two scheduled jobs on the same interval (configured via **Inventory Sync Frequency** in Shopify Setting):

| Job | What it does |
|---|---|
| **Inventory sync** | Pushes stock levels from ERPNext bins to Shopify locations |
| **Item & price sync** | Pushes title, description, and price list rate for all synced items |

The two jobs use separate timestamps (`last_inventory_sync` / `last_item_sync`) so they run independently even though they share the same frequency setting.

---

### Warehouse & Inventory Mapping

To sync stock levels:

1. Enable **Update ERPNext stock levels to Shopify**.
2. Set **Inventory Sync Frequency** (5 / 10 / 15 / 30 / 60 minutes).
3. Click **Fetch Shopify Locations** to pull your Shopify locations into the mapping table.
4. Map each Shopify location to the corresponding ERPNext warehouse.

---

### Order Sync (Shopify → ERPNext)

Orders flow from Shopify into ERPNext automatically via webhooks:

| Shopify event | ERPNext document created |
|---|---|
| Order created | Sales Order |
| Order paid | Sales Invoice |
| Order fulfilled | Delivery Note |
| Order cancelled | Cancellation of above documents |

Configure the document series, tax mappings, and shipping account in **Shopify Setting**.

---

### Installation

```bash
# install app
bench get-app ecommerce_integrations --branch version-15

# install on site
bench --site sitename install-app ecommerce_integrations

# run migrate
bench --site sitename migrate
```

---

### Contributing

- Follow general [ERPNext contribution guidelines](https://github.com/frappe/erpnext/wiki/Contribution-Guidelines).
- Send PRs to the `develop` branch only.

### Development setup

- Enable developer mode.
- Set `localtunnel_url` in your `site_config.json` with your ngrok / localtunnel URL for webhook registration during local development.

#### License

GNU GPL v3.0
