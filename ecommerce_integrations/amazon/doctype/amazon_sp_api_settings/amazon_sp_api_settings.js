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
		}
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
