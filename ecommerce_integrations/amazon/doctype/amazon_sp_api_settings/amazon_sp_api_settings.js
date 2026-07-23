// Copyright (c) 2022, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Amazon SP API Settings", {
	refresh(frm) {
		if (frm.doc.__islocal && !frm.doc.amazon_fields_map) {
			frm.trigger("set_default_fields_map");
		}
		frm.trigger("set_queries");
		frm.set_df_property("amazon_fields_map", "cannot_add_rows", true);
		frm.set_df_property("amazon_fields_map", "cannot_delete_rows", true);

		if (!frm.doc.__islocal) {
			frm.add_custom_button(__("Import Existing Amazon Mappings (CSV)"), () => {
				frm.trigger("import_amazon_mappings_csv");
			});
			frm.add_custom_button(__("Check Listing Status"), () => {
				frm.trigger("check_amazon_listing_status");
			});
			frm.add_custom_button(__("Discover Required Fields"), () => {
				frm.trigger("discover_required_fields");
			});
		}
	},

	discover_required_fields(frm) {
		frappe.prompt(
			[
				{
					fieldname: "product_type",
					fieldtype: "Data",
					label: __("Amazon Product Type"),
					reqd: 1,
					description: __("e.g. HEADPHONES, WEARABLE_COMPUTER — must match an Item's Amazon Product Type field"),
				},
			],
			(values) => {
				frappe.call({
					method: "ecommerce_integrations.amazon.product.describe_product_type_requirements",
					args: { amz_setting_name: frm.docname, product_type: values.product_type },
					freeze: true,
					freeze_message: __("Fetching schema from Amazon…"),
					callback: (r) => {
						if (r.exc) return;
						const rows = r.message || [];
						if (!rows.length) {
							frappe.msgprint(__("No requirements found — check the product type code is correct."));
							return;
						}

						const alwaysRows = rows.filter((r) => r.always_required);
						const conditionalRows = rows.filter((r) => !r.always_required);

						const row_html = (row) => {
							const values_html = row.allowed_values
								? `<br><i>${__("Allowed")}: ${row.allowed_values.join(", ")}</i>`
								: "";
							return `<tr>
								<td><code>${row.name}</code></td>
								<td>${row.title}${values_html}</td>
							</tr>`;
						};

						const table = (title, rowsForTable, note) => `
							<h6>${title} (${rowsForTable.length})</h6>
							${note ? `<p class="text-muted">${note}</p>` : ""}
							<table class="table table-bordered">
								<thead><tr><th>${__("Attribute (JSON key)")}</th><th>${__("What it means")}</th></tr></thead>
								<tbody>${rowsForTable.map(row_html).join("")}</tbody>
							</table>`;

						const html = `<div style="max-height:500px;overflow:auto">
							${table(__("Always Required"), alwaysRows)}
							${table(
								__("Conditionally Required"),
								conditionalRows,
								__("Depend on category/variation specifics — likely relevant but not guaranteed for every item.")
							)}
						</div>
						<p class="text-muted">${__("Use these keys in the Item's 'E-commerce Attributes (JSON)' field.")}</p>`;

						frappe.msgprint({
							title: __("{0} — {1} Fields", [values.product_type, rows.length]),
							message: html,
							wide: true,
						});
					},
				});
			},
			__("Discover Required Fields"),
			__("Fetch")
		);
	},

	check_amazon_listing_status(frm) {
		frappe.call({
			method: "ecommerce_integrations.amazon.product.check_amazon_listing_status",
			args: { amz_setting_name: frm.docname },
			freeze: true,
			freeze_message: __("Checking listings on Amazon…"),
			callback: (r) => {
				if (r.exc) return;
				const rows = r.message || [];
				if (!rows.length) {
					frappe.msgprint(__("No published items found to check."));
					return;
				}

				let hasProblem = false;
				const rows_html = rows
					.map((row) => {
						if (row.error) {
							hasProblem = true;
							return `<tr><td>${row.item_code}</td><td>${row.sku}</td><td>${row.asin || ""}</td>
								<td colspan="2" style="color:#c0392b">${__("API Error")}: ${row.error}</td></tr>`;
						}
						const status = (row.status || []).join(", ") || "-";
						const errors = row.errors || [];
						const warnings = row.warnings || [];
						if (errors.length || row.suppressed) hasProblem = true;
						const errText = errors.length
							? `<span style="color:#c0392b">${errors.join("<br>")}</span>`
							: "";
						const warnText = warnings.length
							? `<span style="color:#b58900">${warnings.join("<br>")}</span>`
							: "";
						return `<tr>
							<td>${row.item_code}</td>
							<td>${row.sku}</td>
							<td>${row.asin || ""}</td>
							<td>${status}${row.suppressed ? ' <b style="color:#c0392b">(SUPPRESSED)</b>' : ""}</td>
							<td>${errText}${errText && warnText ? "<br>" : ""}${warnText}</td>
						</tr>`;
					})
					.join("");

				const html = `<div style="max-height:400px;overflow:auto">
					<table class="table table-bordered">
						<thead><tr><th>${__("Item")}</th><th>${__("SKU")}</th><th>${__("ASIN")}</th>
						<th>${__("Status")}</th><th>${__("Issues")}</th></tr></thead>
						<tbody>${rows_html}</tbody>
					</table></div>`;

				frappe.msgprint({
					title: __("Amazon Listing Status ({0} checked)", [rows.length]),
					message: html,
					wide: true,
					indicator: hasProblem ? "orange" : "green",
				});
			},
		});
	},

	import_amazon_mappings_csv(frm) {
		new frappe.ui.FileUploader({
			doctype: frm.doctype,
			docname: frm.docname,
			folder: "Home/Attachments",
			restrictions: {
				allowed_file_types: [".csv"],
			},
			on_success: (file_doc) => {
				frappe.show_alert({ message: __("Uploaded, importing mappings…"), indicator: "blue" });
				frappe.call({
					method: "ecommerce_integrations.amazon.product.import_existing_amazon_mappings_from_csv",
					args: { file_path: file_doc.file_url },
					freeze: true,
					freeze_message: __("Importing mappings…"),
					callback: (r) => {
						if (r.exc) return;
						const result = r.message || {};
						const skipped = result.skipped || [];
						let msg = __("Created: {0}, Updated: {1}", [result.created || 0, result.updated || 0]);
						if (skipped.length) {
							msg += `<br><br><b>${__("Skipped")} (${skipped.length}):</b><br>`;
							msg += skipped
								.map((s) => `${s.item_code || "?"}: ${s.reason}`)
								.join("<br>");
						}
						frappe.msgprint({
							title: __("Import Result"),
							message: msg,
							indicator: skipped.length ? "orange" : "green",
						});
					},
				});
			},
		});
	},

	set_default_fields_map(frm) {
		frappe.call({
			method: "set_default_fields_map",
			doc: frm.doc,
			callback: (r) => {
				if (!r.exc) refresh_field("amazon_fields_map");
			},
		});
	},

	set_queries(frm) {
		frm.set_query("warehouse", () => {
			return {
				filters: {
					is_group: 0,
					company: frm.doc.company,
				},
			};
		});

		frm.set_query("market_place_account_group", () => {
			return {
				filters: {
					is_group: 1,
					company: frm.doc.company,
				},
			};
		});
	},
});
