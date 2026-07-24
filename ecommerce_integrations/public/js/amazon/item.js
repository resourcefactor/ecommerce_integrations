// Copyright (c) 2026, Frappe and contributors
// For license information, please see LICENSE

// Fields the publish flow (amazon/product.py:_build_listing_attributes)
// already auto-derives from other Item fields — pre-fill from the real Item
// data instead of a generic Amazon example, since we already have it.
// Returns null (not empty string) when there's no real value yet, so the
// caller falls through to Amazon's example instead of leaving the row blank.
function amazon_auto_derived_value(frm, parameter) {
	if (parameter === "brand") return frm.doc.brand || null;
	if (parameter === "bullet_point") {
		return frm.doc.description ? frappe.utils.html2text(frm.doc.description) : null;
	}
	// list_price is filled from Item Price at publish time, not a single Item
	// field — nothing to show here, always fall back to Amazon's example.
	return null;
}

// Shared helper: resolve the active Amazon SP API Settings record and pass its
// name to `callback`. Used by both Fetch Required Fields and Search Product
// Type. Goes through a whitelisted server method rather than querying the
// doctype directly — regular users (e.g. stock/sales roles editing Items)
// don't have read permission on Amazon SP API Settings, which holds API
// credentials and is restricted to System Manager.
function with_active_amazon_setting(callback) {
	frappe.call({
		method: "ecommerce_integrations.amazon.product.get_active_amazon_setting",
		callback: (r) => {
			if (r.exc || !r.message) return;
			callback(r.message.name);
		},
	});
}

frappe.ui.form.on("Item", {
	refresh(frm) {
		frm.trigger("render_amazon_publish_status");

		const grid = frm.get_field("ecommerce_attributes")?.grid;
		if (!grid) return;

		grid.add_custom_button(__("Check Allowed Values"), () => {
			const selected = grid.get_selected_children();
			if (!selected.length) {
				frappe.msgprint(__("Select one or more rows first (check the row, then click this button)."));
				return;
			}
			if (!frm.doc.amazon_product_type) {
				frappe.msgprint(__("Please set Amazon Product Type first."));
				return;
			}

			with_active_amazon_setting((amz_setting_name) => {
				const lookups = selected.map((row) =>
					frappe.call({
						method: "ecommerce_integrations.amazon.product.get_attribute_allowed_values",
						args: {
							amz_setting_name: amz_setting_name,
							product_type: frm.doc.amazon_product_type,
							parameter: row.parameter,
						},
					}).then((r) => r.message)
				);

				Promise.all(lookups).then((results) => {
					const rows_html = results
						.map((res) => {
							if (!res) return "";
							if (res.error) {
								return `<tr><td><code>${res.name}</code></td><td colspan="2" style="color:#c0392b">${res.error}</td></tr>`;
							}
							const values = res.allowed_values
								? res.allowed_values.join(", ")
								: __("No fixed list — free text");
							return `<tr>
								<td><code>${res.name}</code></td>
								<td>${res.title}</td>
								<td>${values}</td>
							</tr>`;
						})
						.join("");

					frappe.msgprint({
						title: __("Allowed Values"),
						wide: true,
						message: `<div style="max-height:400px;overflow:auto">
							<table class="table table-bordered">
								<thead><tr><th>${__("Parameter")}</th><th>${__("Name")}</th><th>${__("Allowed Values")}</th></tr></thead>
								<tbody>${rows_html}</tbody>
							</table>
						</div>`,
					});
				});
			});
		});
	},

	fetch_amazon_attributes(frm) {
		if (!frm.doc.amazon_product_type) {
			frappe.msgprint(__("Please set Amazon Product Type first."));
			return;
		}

		with_active_amazon_setting((amz_setting_name) => {
			frappe.call({
				method: "ecommerce_integrations.amazon.product.describe_product_type_requirements",
				args: { amz_setting_name: amz_setting_name, product_type: frm.doc.amazon_product_type },
				freeze: true,
				freeze_message: __("Fetching required fields from Amazon…"),
				callback: (r) => {
					if (r.exc) return;
					const rows = r.message || [];
					if (!rows.length) {
						frappe.msgprint(__("No requirements found — check the product type code is correct."));
						return;
					}

					const existing = new Set((frm.doc.ecommerce_attributes || []).map((row) => row.parameter));
					let added = 0;

					rows.forEach((row) => {
						if (existing.has(row.name)) return; // don't clobber a value already filled in

						const auto_value = amazon_auto_derived_value(frm, row.name);
						const value = auto_value !== null ? auto_value : row.example || "";

						frm.add_child("ecommerce_attributes", {
							parameter: row.name,
							value: value,
							allowed_values: row.allowed_values ? row.allowed_values.join(", ") : "",
						});
						added++;
					});

					frm.refresh_field("ecommerce_attributes");
					frappe.msgprint({
						title: __("Fields Added"),
						indicator: "orange",
						message: __(
							"Added {0} new parameter row(s) ({1} already present were kept as-is). " +
								"Rows without a real Item value are pre-filled with Amazon's own example " +
								"or an allowed value as a placeholder — review and replace them with " +
								"actual data before publishing.",
							[added, rows.length - added]
						),
					});
				},
			});
		});
	},

	search_amazon_product_type(frm) {
		frappe.prompt(
			[
				{
					fieldname: "keywords",
					fieldtype: "Data",
					label: __("Keywords"),
					reqd: 1,
					description: __("e.g. headphones, smart watch"),
				},
			],
			(values) => {
				with_active_amazon_setting((amz_setting_name) => {
					frappe.call({
						method: "ecommerce_integrations.amazon.product.search_amazon_product_types",
						args: { amz_setting_name: amz_setting_name, keywords: values.keywords },
						freeze: true,
						freeze_message: __("Searching Amazon…"),
						callback: (r) => {
							if (r.exc) return;
							const rows = r.message || [];
							if (!rows.length) {
								frappe.msgprint(__("No matching product types found — try different keywords."));
								return;
							}

							const dialog = new frappe.ui.Dialog({
								title: __("{0} Matching Product Types", [rows.length]),
								size: "small",
								fields: [{ fieldname: "results_html", fieldtype: "HTML" }],
							});

							const rows_html = rows
								.map(
									(row, idx) => `<tr class="amazon-pt-row" data-idx="${idx}" style="cursor:pointer">
										<td><code>${row.name}</code></td>
										<td>${row.display_name}</td>
									</tr>`
								)
								.join("");

							dialog.fields_dict.results_html.$wrapper.html(`
								<div style="max-height:400px;overflow:auto">
									<table class="table table-bordered">
										<thead><tr><th>${__("Code")}</th><th>${__("Name")}</th></tr></thead>
										<tbody>${rows_html}</tbody>
									</table>
								</div>
								<p class="text-muted">${__("Click a row to set Amazon Product Type.")}</p>
							`);

							dialog.$wrapper.find(".amazon-pt-row").on("click", function () {
								const idx = $(this).data("idx");
								frm.set_value("amazon_product_type", rows[idx].name);
								dialog.hide();
							});

							dialog.show();
						},
					});
				});
			},
			__("Search Amazon Product Type"),
			__("Search")
		);
	},

	render_amazon_publish_status(frm) {
		const $wrapper = frm.get_field("custom_item_publish_error")?.$wrapper;
		if (!$wrapper) return;

		if (frm.doc.__islocal) {
			$wrapper.html("");
			return;
		}

		$wrapper.html(`<div class="text-muted">${__("Loading Amazon publish status…")}</div>`);

		frappe.call({
			method: "ecommerce_integrations.amazon.product.get_item_publish_status",
			args: { item_code: frm.doc.name },
			callback: (r) => {
				const info = r.message || {};

				if (!info.status && !info.error) {
					$wrapper.html(`<div class="text-muted">${__("Not yet synced to Amazon.")}</div>`);
					return;
				}

				const synced_on = info.synced_on
					? frappe.datetime.str_to_user(info.synced_on)
					: __("Unknown");

				const status_color = info.status === "Error" ? "#c0392b" : info.status === "Synced" ? "#2e7d32" : "#8a6d00";

				let html = `<div>
					<b>${__("Status")}:</b> <span style="color:${status_color}">${info.status || __("Unknown")}</span>
					<span class="text-muted"> — ${__("last attempt")}: ${synced_on}</span>
				</div>`;

				if (info.error) {
					html += `<div style="margin-top:6px;color:#c0392b;white-space:pre-wrap">${frappe.utils.escape_html(info.error)}</div>`;
				}

				$wrapper.html(html);
			},
		});
	},
});
