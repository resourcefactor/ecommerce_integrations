// Copyright (c) 2021, Frappe and contributors
// For license information, please see LICENSE

frappe.provide("ecommerce_integrations.shopify.shopify_setting");

frappe.ui.form.on("Shopify Setting", {
	onload: function (frm) {
		frappe.call({
			method: "ecommerce_integrations.utils.naming_series.get_series",
			callback: function (r) {
				$.each(r.message, (key, value) => {
					set_field_options(key, value);
				});
			},
		});
	},

	fetch_shopify_locations: function (frm) {
		frappe.call({
			doc: frm.doc,
			method: "update_location_table",
			callback: (r) => {
				if (!r.exc) refresh_field("shopify_warehouse_mapping");
			},
		});
	},

	refresh: function (frm) {
		frm.add_custom_button(__("View Logs"), () => {
			frappe.set_route("List", "Ecommerce Integration Log", {
				integration: "Shopify",
			});
		});
		frm.add_custom_button(__("Sync Price to Shopify"), function () {
			frappe.call({
				doc: frm.doc,
				method: "sync_price_to_shopify",
				callback: (r) => {
					if (!r.exc) frappe.msgprint(r.message);
				},
			});
		});
		frm.add_custom_button(__("Sync Orders Now"), function () {
			frappe.call({
				doc: frm.doc,
				method: "sync_orders_now",
				freeze: true,
				freeze_message: __("Queuing order sync..."),
				callback: (r) => {
					if (!r.exc) frappe.msgprint(r.message);
				},
			});
		}, __("Shopify"));
		frm.add_custom_button(__("Export Shopify Products"), function () {
			frappe.show_alert({ message: __("Fetching products from Shopify..."), indicator: "blue" });
			frappe.call({
				doc: frm.doc,
				method: "fetch_shopify_items_for_mapping",
				callback: function (r) {
					if (!r.message || !r.message.length) {
						frappe.msgprint(__("No products found in Shopify."));
						return;
					}
					const headers = ["Shopify Product ID", "Product Title", "Variant ID", "Variant Title", "SKU", "ERPNext Item Code"];
					const csvContent = [headers, ...r.message.map(row => [
						row.shopify_product_id, row.product_title, row.variant_id,
						row.variant_title, row.sku, row.erpnext_item_code,
					])].map(row => row.map(cell => `"${String(cell || "").replace(/"/g, '""')}"`).join(",")).join("\n");

					const blob = new Blob([csvContent], { type: "text/csv" });
					const url = URL.createObjectURL(blob);
					const a = document.createElement("a");
					a.href = url; a.download = "shopify_products_mapping.csv"; a.click();
					URL.revokeObjectURL(url);
					frappe.show_alert({ message: __("{0} variants exported", [r.message.length]), indicator: "green" });
				},
			});
		}, __("Shopify"));
		frm.add_custom_button(__("Import Item Mapping"), function () {
			const input = document.createElement("input");
			input.type = "file";
			input.accept = ".csv";
			input.onchange = function () {
				const reader = new FileReader();
				reader.onload = function (e) {
					frappe.call({
						doc: frm.doc,
						method: "import_shopify_item_mapping",
						args: { csv_data: e.target.result },
						callback: function (r) {
							const res = r.message;
							let msg = `<b>Created:</b> ${res.created}<br><b>Skipped:</b> ${res.skipped}`;
							if (res.errors.length) {
								msg += `<br><br><b>Errors (${res.errors.length}):</b><br>` + res.errors.slice(0, 20).join("<br>");
							}
							frappe.msgprint({ title: __("Import Result"), message: msg, indicator: res.errors.length ? "orange" : "green" });
						},
					});
				};
				reader.readAsText(input.files[0]);
			};
			input.click();
		}, __("Shopify"));
		frm.trigger("setup_queries");
	},

	setup_queries: function (frm) {
		const warehouse_query = () => {
			return {
				filters: {
					company: frm.doc.company,
					is_group: 0,
					disabled: 0,
				},
			};
		};
		frm.set_query("warehouse", warehouse_query);
		frm.set_query(
			"erpnext_warehouse",
			"shopify_warehouse_mapping",
			warehouse_query,
		);

		frm.set_query("price_list", () => {
			return {
				filters: {
					selling: 1,
				},
			};
		});

		frm.set_query("cost_center", () => {
			return {
				filters: {
					company: frm.doc.company,
					is_group: "No",
				},
			};
		});

		frm.set_query("cash_bank_account", () => {
			return {
				filters: [
					["Account", "account_type", "in", ["Cash", "Bank"]],
					["Account", "root_type", "=", "Asset"],
					["Account", "is_group", "=", 0],
					["Account", "company", "=", frm.doc.company],
				],
			};
		});

		const tax_query = () => {
			return {
				query: "erpnext.controllers.queries.tax_account_query",
				filters: {
					account_type: ["Tax", "Chargeable", "Expense Account"],
					company: frm.doc.company,
				},
			};
		};

		frm.set_query("tax_account", "taxes", tax_query);
		frm.set_query("default_sales_tax_account", tax_query);
		frm.set_query("default_shipping_charges_account", tax_query);
	},
});
