frappe.listview_settings["Sync Run"] = {
	add_fields: [
		"status",
		"sync_definition",
		"processed_count",
		"success_count",
		"error_count",
		"conflict_count",
		"finished_at",
		"summary",
	],

	onload(listview) {
		listview.page.add_inner_button(__("Errors"), () => {
			listview.filter_area.clear(false);
			listview.filter_area.add("Sync Run", "error_count", ">", 0);
		});
		listview.page.add_inner_button(__("Conflicts"), () => {
			listview.filter_area.clear(false);
			listview.filter_area.add("Sync Run", "conflict_count", ">", 0);
		});
		listview.page.add_inner_button(__("Active"), () => {
			listview.filter_area.clear(false);
			listview.filter_area.add("Sync Run", "status", "in", ["Queued", "Running"]);
		});
	},

	get_indicator(doc) {
		const status = doc.status || __("Unknown");
		const summary = sync.run_list.buildStatusSummary(doc);
		return [summary ? `${status} · ${summary}` : status, sync.run_list.indicatorColor(status), "status,=," + status];
	},

	formatters: {
		summary(value, _field, doc) {
			const text = value || sync.run_list.buildCounterText(doc);
			if (!text) {
				return `<span class="text-muted">${__("No summary yet")}</span>`;
			}
			return `<span title="${frappe.utils.escape_html(String(value || ""))}">${frappe.utils.escape_html(sync.run_list.truncate(text, 90))}</span>`;
		},
		},

	button: {
		show(doc) {
			return Boolean(["Error", "Partial Error", "Needs Review"].includes(doc.status) || cint(doc.error_count) > 0 || cint(doc.conflict_count) > 0);
		},
		get_label() {
			return __("Open Items");
		},
		get_description(doc) {
			return __("Open run items for {0}", [doc.name]);
		},
		action(doc) {
			frappe.set_route("List", "Sync Run Item", {
				sync_run: doc.name,
			});
		},
	},
};

frappe.provide("sync.run_list");

sync.run_list.buildStatusSummary = function (doc) {
	const parts = [];
	const processed = cint(doc.processed_count);
	const success = cint(doc.success_count);
	const errors = cint(doc.error_count);
	const conflicts = cint(doc.conflict_count);
	if (processed) {
		parts.push(__("{0} processed", [processed]));
	}
	if (success) {
		parts.push(__("{0} ok", [success]));
	}
	if (errors) {
		parts.push(__("{0} errors", [errors]));
	}
	if (conflicts) {
		parts.push(__("{0} conflicts", [conflicts]));
	}
	return parts.join(", ");
};

sync.run_list.buildCounterText = function (doc) {
	const parts = [];
	for (const fieldname of ["processed_count", "success_count", "error_count", "conflict_count"]) {
		const value = cint(doc[fieldname]);
		if (!value) {
			continue;
		}
		parts.push(`${fieldname.replace(/_count$/, "")}=${value}`);
	}
	return parts.join(", ");
};

sync.run_list.indicatorColor = function (status) {
	const key = String(status || "").toLowerCase();
	if (key === "success") {
		return "green";
	}
	if (key === "error") {
		return "red";
	}
	if (key === "partial error") {
		return "red";
	}
	if (key === "needs review") {
		return "orange";
	}
	if (["queued", "running"].includes(key)) {
		return "blue";
	}
	return "gray";
};

sync.run_list.truncate = function (value, maxLength) {
	const text = String(value || "");
	if (text.length <= maxLength) {
		return text;
	}
	return `${text.slice(0, maxLength - 1)}…`;
};
