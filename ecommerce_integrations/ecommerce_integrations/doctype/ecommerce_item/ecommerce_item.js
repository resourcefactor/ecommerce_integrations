// Copyright (c) 2021, Frappe and contributors
// For license information, please see LICENSE

const SYNC_METHOD_BY_INTEGRATION = {
	amazon: "ecommerce_integrations.amazon.product.sync_item_to_amazon",
	shopify: "ecommerce_integrations.shopify.product.sync_item_to_shopify",
};

frappe.ui.form.on("Ecommerce Item", {
	refresh(frm) {
		if (frm.doc.__islocal) return;

		const method = SYNC_METHOD_BY_INTEGRATION[(frm.doc.integration || "").toLowerCase()];
		if (!method) return;

		frm.add_custom_button(__("Sync Now"), () => {
			frappe.call({
				method: method,
				args: { ecommerce_item: frm.doc.name },
				freeze: true,
				freeze_message: __("Syncing to {0}…", [frm.doc.integration]),
				callback: (r) => {
					if (r.exc) return;
					const res = r.message || {};
					frm.reload_doc();

					const indicator = res.sync_status === "Error" ? "red" : "green";
					let message = __("Sync status: {0}", [res.sync_status || __("Unknown")]);
					message += `<br>${__("Stock/price/images")}: ${res.stock_result || __("skipped")}`;
					if (res.sync_error) {
						message += `<br><br><span style="color:#c0392b">${frappe.utils.escape_html(res.sync_error)}</span>`;
					}

					frappe.msgprint({
						title: __("Sync Now — Result"),
						indicator: indicator,
						message: message,
					});
				},
			});
		});
	},
});
