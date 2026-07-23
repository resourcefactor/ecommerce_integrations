// Copyright (c) 2026, Frappe and contributors
// For license information, please see LICENSE

// Shared helper: resolve the active Amazon SP API Settings record and pass its
// name to `callback`. Used by both Fetch Required Fields and Search Product
// Type so there's one place that handles "none found" / "multiple found".
function with_active_amazon_setting(callback) {
	frappe.db
		.get_list("Amazon SP API Settings", {
			filters: { is_active: 1 },
			fields: ["name"],
			limit: 2,
		})
		.then((settings) => {
			if (!settings.length) {
				frappe.msgprint(__("No active Amazon SP API Settings found."));
				return;
			}
			if (settings.length > 1) {
				frappe.msgprint(
					__("Multiple active Amazon SP API Settings found — using {0}.", [settings[0].name])
				);
			}
			callback(settings[0].name);
		});
}

frappe.ui.form.on("Item", {
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
						frm.add_child("ecommerce_attributes", {
							parameter: row.name,
							allowed_values: row.allowed_values ? row.allowed_values.join(", ") : "",
						});
						added++;
					});

					frm.refresh_field("ecommerce_attributes");
					frappe.show_alert({
						message: __("Added {0} new parameter row(s) ({1} already present were kept as-is).", [
							added,
							rows.length - added,
						]),
						indicator: "green",
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
});
