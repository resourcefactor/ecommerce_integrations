// Copyright (c) 2026, Frappe and contributors
// For license information, please see LICENSE

const TAKEALOT_MAPPING_COLUMNS = [
	"offer_id",
	"sku",
	"barcode",
	"title",
	"status",
	"sold_30_days",
	"suggested_item_code",
	"item_code",
	"pack_qty",
];

frappe.ui.form.on("Takealot Settings", {
	onload(frm) {
		frappe.call({
			method: "ecommerce_integrations.utils.naming_series.get_series",
			callback(r) {
				$.each(r.message, (key, value) => set_field_options(key, value));
			},
		});
	},

	refresh(frm) {
		frm.trigger("setup_queries");

		frm.add_custom_button(__("View Logs"), () => {
			frappe.set_route("List", "Ecommerce Integration Log", { integration: "Takealot" });
		});
		if (!frm.doc.enable_takealot) return;

		const group = __("Takealot");
		const run = (method, args = {}, freeze_message = null) => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Please save the settings first."));
				return Promise.resolve();
			}
			return frappe.call({
				doc: frm.doc,
				method,
				args,
				freeze: Boolean(freeze_message),
				freeze_message,
			});
		};

		frm.add_custom_button(
			__("Test Connection"),
			() =>
				run("test_connection", {}, __("Connecting to Takealot…")).then((r) => {
					if (r && r.message) frappe.msgprint({ message: r.message, indicator: "green" });
				}),
			group
		);
		frm.add_custom_button(
			__("Fetch Seller Warehouses"),
			() =>
				run("fetch_seller_warehouses", {}, __("Fetching seller warehouses…")).then((r) => {
					if (!r || !r.message) return;
					frm.refresh_field("seller_warehouses");
					frm.scroll_to_field("seller_warehouses");
					const { fetched, added } = r.message;
					if (added) frm.dirty();
					frappe.show_alert({
						message: added
							? __("{0} seller warehouses added. Map them to ERPNext warehouses and save.", [added])
							: __("{0} seller warehouses on Takealot, all already listed.", [fetched]),
						indicator: "green",
					});
				}),
			group
		);
		frm.add_custom_button(__("Import Offers"), () => run("import_offers"), group);
		frm.add_custom_button(
			__("Export Unmapped Offers"),
			() =>
				run("export_unmapped_offers", {}, __("Reading offers from Takealot…")).then((r) => {
					if (!r) return;
					const rows = r.message || [];
					if (!rows.length) {
						frappe.msgprint(__("Every Takealot offer is mapped to an ERPNext item."));
						return;
					}
					download_csv(rows, "takealot_unmapped_offers.csv");
				}),
			group
		);
		frm.add_custom_button(__("Import Offer Mapping"), () => import_offer_mapping(frm, run), group);
		frm.add_custom_button(__("Sync Orders Now"), () => run("sync_orders_now"), group);
		frm.add_custom_button(__("Sync Returns Now"), () => run("sync_returns_now"), group);
		if (frm.doc.push_price || frm.doc.push_stock) {
			frm.add_custom_button(__("Push Changed Offers Now"), () => run("push_offers_now"), group);
		}
	},

	setup_queries(frm) {
		const warehouse_query = () => ({
			filters: { company: frm.doc.company, is_group: 0, disabled: 0 },
		});
		["warehouse", "leadtime_warehouse", "return_warehouse", "removal_warehouse"].forEach((field) =>
			frm.set_query(field, warehouse_query)
		);
		frm.set_query("warehouse", "region_warehouses", warehouse_query);
		frm.set_query("erpnext_warehouse", "seller_warehouses", warehouse_query);

		const account_query = () => ({
			filters: { company: frm.doc.company, is_group: 0, disabled: 0 },
		});
		[
			"vat_account",
			"fee_vat_account",
			"success_fee_account",
			"fulfillment_fee_account",
			"courier_collection_fee_account",
			"stock_transfer_fee_account",
		].forEach((field) => frm.set_query(field, account_query));

		frm.set_query("cost_center", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("price_list", () => ({ filters: { selling: 1, enabled: 1 } }));
	},
});

function download_csv(rows, filename) {
	const escape = (value) => `"${String(value ?? "").replace(/"/g, '""')}"`;
	const csv = [TAKEALOT_MAPPING_COLUMNS, ...rows.map((row) => TAKEALOT_MAPPING_COLUMNS.map((c) => row[c]))]
		.map((row) => row.map(escape).join(","))
		.join("\n");

	const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
	const link = document.createElement("a");
	link.href = url;
	link.download = filename;
	link.click();
	URL.revokeObjectURL(url);
	frappe.show_alert({ message: __("{0} unmapped offers exported", [rows.length]), indicator: "green" });
}

function import_offer_mapping(frm, run) {
	// the input has to be in the document, or some browsers never fire change on it
	const input = document.createElement("input");
	input.type = "file";
	input.accept = ".csv";
	input.style.display = "none";
	document.body.appendChild(input);
	input.onchange = () => {
		input.remove();
		if (!input.files.length) return;
		const reader = new FileReader();
		reader.onload = (e) =>
			run("import_offer_mapping", { csv_data: e.target.result }, __("Importing mapping…")).then((r) => {
				if (!r || !r.message) return;
				const res = r.message;
				let message = __("Created: {0}<br>Updated: {1}<br>Skipped (no item_code): {2}", [
					res.created,
					res.updated,
					res.skipped,
				]);
				if (res.errors.length) {
					message += `<br><br><b>${__("Errors ({0})", [res.errors.length])}</b><br>`;
					message += res.errors.slice(0, 20).map(frappe.utils.escape_html).join("<br>");
				}
				frappe.msgprint({
					title: __("Import Offer Mapping"),
					message,
					indicator: res.errors.length ? "orange" : "green",
				});
			});
		reader.readAsText(input.files[0]);
	};
	input.click();
}
