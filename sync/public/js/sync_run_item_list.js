frappe.listview_settings["Sync Run Item"] = {
	add_fields: ["action", "status", "sync_definition", "document_name", "message", "record_key"],

	onload(listview) {
		listview.page.add_inner_button(__("Errors"), () => {
			listview.filter_area.clear(false);
			listview.filter_area.add("Sync Run Item", "status", "=", "error");
		});
		listview.page.add_inner_button(__("Conflicts"), () => {
			listview.filter_area.clear(false);
			listview.filter_area.add("Sync Run Item", "status", "=", "conflict");
		});
		listview.page.add_inner_button(__("Skipped"), () => {
			listview.filter_area.clear(false);
			listview.filter_area.add("Sync Run Item", "status", "=", "skipped");
		});
	},

	get_indicator(doc) {
		const label = [doc.action, doc.status].filter(Boolean).join(" / ") || __("Unknown");
		return [label, sync.run_item_list.indicatorColor(doc), "status,=," + (doc.status || "")];
	},

	formatters: {
		document_name(value, _field, doc) {
			if (!value) {
				return `<span class="text-muted">${__("Not linked")}</span>`;
			}
			return `<span title="${frappe.utils.escape_html(String(value))}">${frappe.utils.escape_html(String(value))}</span>`;
		},
		message(value) {
			if (!value) {
				return `<span class="text-muted">${__("No message")}</span>`;
			}
			return `<span title="${frappe.utils.escape_html(String(value))}">${frappe.utils.escape_html(sync.run_item_list.truncate(value, 80))}</span>`;
		},
		record_key(value) {
			if (!value) {
				return `<span class="text-muted">${__("No record key")}</span>`;
			}
			return `<strong title="${frappe.utils.escape_html(String(value))}">${frappe.utils.escape_html(sync.run_item_list.truncate(value, 50))}</strong>`;
		},
	},

	button: {
		show(doc) {
			return Boolean(doc.document_name && doc.sync_definition);
		},
		get_label() {
			return __("Open Document");
		},
		get_description(doc) {
			return __("Resolve the Sync Definition target DocType and open {0}", [doc.document_name]);
		},
		action(doc) {
			frappe.db
				.get_value("Sync Definition", doc.sync_definition, "doctype_name")
				.then((response) => {
					const payload = response?.message;
					const doctypeName = typeof payload === "string" ? payload : payload?.doctype_name || "";
					if (!doctypeName) {
						frappe.show_alert({ message: __("No target DocType configured on the Sync Definition."), indicator: "orange" });
						return;
					}
					frappe.set_route("Form", doctypeName, doc.document_name);
				})
				.catch((error) => {
					frappe.show_alert({
						message: error?.message || __("Unable to resolve target DocType."),
						indicator: "red",
					});
				});
		},
	},
};

frappe.provide("sync.run_item_list");

sync.run_item_list.indicatorColor = function (doc) {
	const action = String(doc.action || "").toLowerCase();
	const status = String(doc.status || "").toLowerCase();
	if (status === "error" || action === "error" || action === "conflict") {
		return "red";
	}
	if (status === "success" && ["created", "updated"].includes(action)) {
		return "green";
	}
	if (status === "skipped" || action === "skipped") {
		return "orange";
	}
	return "blue";
};

sync.run_item_list.truncate = function (value, maxLength) {
	const text = String(value || "");
	if (text.length <= maxLength) {
		return text;
	}
	return `${text.slice(0, maxLength - 1)}…`;
};
