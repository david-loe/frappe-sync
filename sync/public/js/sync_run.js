frappe.provide("sync.run");

frappe.ui.form.on("Sync Run", {
	refresh(frm) {
		sync.run.setupButtons(frm);
		sync.run.renderHealth(frm);
		sync.run.renderItems(frm);
	},
	sync_definition(frm) {
		sync.run.clearCaches(frm);
		sync.run.renderHealth(frm);
		sync.run.renderItems(frm);
	},
});

sync.run.clearCaches = function (frm) {
	frm.__sync_run_health_render_id = (frm.__sync_run_health_render_id || 0) + 1;
	frm.__sync_run_items_render_id = (frm.__sync_run_items_render_id || 0) + 1;
};

sync.run.setupButtons = function (frm) {
	frm.clear_custom_buttons();

	frm.add_custom_button(__("Refresh Monitoring"), () => {
		sync.run.renderHealth(frm, { force: true });
		sync.run.renderItems(frm, { force: true });
	});

	if (!frm.is_new()) {
		frm.add_custom_button(__("Error Items"), () => {
			frappe.set_route("List", "Sync Run Item", {
				sync_run: frm.doc.name,
				status: "error",
			});
		});
		frm.add_custom_button(__("Conflict Items"), () => {
			frappe.set_route("List", "Sync Run Item", {
				sync_run: frm.doc.name,
				status: "conflict",
			});
		});
	}
};

sync.run.getDefinitionDoctype = function (frm) {
	return sync.helpers.getSyncDefinitionDoctype(frm.doc.sync_definition);
};

sync.run.fetchItems = function (frm) {
	return sync.helpers.getList("Sync Run Item", {
		fields: ["name", "record_key", "document_name", "action", "status", "message", "creation"],
		filters: { sync_run: frm.doc.name },
		order_by: "creation desc",
		limit: 50,
	});
};

sync.run.renderHealth = function (frm) {
	const field = frm.get_field("run_health_html");
	if (!field || !field.$wrapper) {
		return;
	}

	if (frm.is_new()) {
		field.$wrapper.html(`<div class="text-muted">${__("Save the run to load monitoring details.")}</div>`);
		return;
	}

	const renderId = (frm.__sync_run_health_render_id || 0) + 1;
	frm.__sync_run_health_render_id = renderId;
	field.$wrapper.html(`<div class="text-muted">${__("Loading monitoring summary…")}</div>`);

	Promise.all([sync.run.getDefinitionDoctype(frm), sync.run.fetchItems(frm)])
		.then(([doctypeName, items]) => {
			if (renderId !== frm.__sync_run_health_render_id) {
				return;
			}
			field.$wrapper.html(sync.run.buildHealthHtml(frm, doctypeName, items));
			sync.run.bindHealthHandlers(frm, doctypeName);
		})
		.catch((error) => {
			if (renderId !== frm.__sync_run_health_render_id) {
				return;
			}
			field.$wrapper.html(
				`<div class="text-danger">${frappe.utils.escape_html(error?.message || __("Unable to load monitoring details."))}</div>`
			);
		});
};

sync.run.renderItems = function (frm, opts = {}) {
	const field = frm.get_field("run_items_html");
	if (!field || !field.$wrapper) {
		return;
	}

	if (frm.is_new()) {
		field.$wrapper.html(`<div class="text-muted">${__("Save the run to load related items.")}</div>`);
		return;
	}

	const renderId = (frm.__sync_run_items_render_id || 0) + 1;
	frm.__sync_run_items_render_id = renderId;
	field.$wrapper.html(`<div class="text-muted">${__("Loading run items…")}</div>`);

	Promise.all([sync.run.getDefinitionDoctype(frm), sync.run.fetchItems(frm)])
		.then(([doctypeName, items]) => {
			if (renderId !== frm.__sync_run_items_render_id) {
				return;
			}
			field.$wrapper.html(sync.run.buildHtml(frm, doctypeName, items, opts));
			sync.run.bindRowHandlers(frm, doctypeName);
		})
		.catch((error) => {
			if (renderId !== frm.__sync_run_items_render_id) {
				return;
			}
			field.$wrapper.html(
				`<div class="text-danger">${frappe.utils.escape_html(error?.message || __("Unable to load run items."))}</div>`
			);
		});
};

sync.run.buildHealthHtml = function (frm, doctypeName, items) {
	const doc = frm.doc;
	const counters = [
		{ label: __("Processed"), value: cint(doc.processed_count), color: "gray" },
		{ label: __("Success"), value: cint(doc.success_count), color: "green" },
		{ label: __("Errors"), value: cint(doc.error_count), color: "red" },
		{ label: __("Conflicts"), value: cint(doc.conflict_count), color: "red" },
		{ label: __("Skipped"), value: cint(doc.skipped_count), color: "orange" },
		{ label: __("Created"), value: cint(doc.created_count), color: "green" },
		{ label: __("Updated"), value: cint(doc.updated_count), color: "blue" },
		{ label: __("Deleted"), value: cint(doc.deleted_count), color: "orange" },
	];
	const issueItems = items.filter((item) => sync.run.isIssueItem(item)).slice(0, 5);
	const statusTone = sync.run.indicatorColor(doc.status);
	const timingLines = [
		sync.run.detailLine(__("Started"), doc.started_at || __("Not started")),
		sync.run.detailLine(__("Finished"), doc.finished_at || __("Still running")),
		sync.run.detailLine(__("Last Sync"), doc.last_sync_at || __("Not recorded")),
		sync.run.detailLine(__("Trigger"), doc.trigger_type || __("Unknown")),
		sync.run.detailLine(__("Dry Run"), doc.dry_run ? __("Yes") : __("No")),
		doc.job_id ? sync.run.detailLine(__("Job ID"), doc.job_id) : "",
	].filter(Boolean);
	const summaryText = doc.summary || sync.run.buildCounterSummary(doc) || __("No summary yet.");
	const errorText = doc.error_message || "";
	const issueHint =
		issueItems.length || cint(doc.error_count) || cint(doc.conflict_count)
			? __("Latest issues are shown below.")
			: __("No recent issue items in the latest loaded slice.");

	return [
		`<div class="row">`,
		`<div class="col-sm-7 mb-3">`,
		`<div class="border rounded p-3 h-100">`,
		`<div class="d-flex flex-wrap justify-content-between align-items-start gap-2 mb-2">`,
		`<div><div class="text-muted small">${__("Run Health")}</div><div>${sync.run.indicator(doc.status || __("Unknown"))}</div></div>`,
		doctypeName
			? `<div class="text-muted small">${frappe.utils.escape_html(__("Target DocType: {0}", [doctypeName]))}</div>`
			: `<div class="text-warning small">${frappe.utils.escape_html(__("Target DocType unresolved"))}</div>`,
		`</div>`,
		`<div class="small text-muted mb-2">${frappe.utils.escape_html(issueHint)}</div>`,
		`<div class="row">`,
		...counters.map((counter) => sync.run.metricCard(counter.label, counter.value, counter.color)),
		`</div>`,
		`</div>`,
		`</div>`,
		`<div class="col-sm-5 mb-3">`,
		`<div class="border rounded p-3 h-100">`,
		`<div class="text-muted small mb-2">${__("Execution Context")}</div>`,
		`<div class="mb-3">${timingLines.join("")}</div>`,
		`<div class="small text-muted mb-1">${__("Summary")}</div>`,
		`<div class="mb-0">${frappe.utils.escape_html(summaryText)}</div>`,
		errorText
			? `<div class="mt-3"><div class="small text-danger mb-1">${__("Run Error")}</div><div class="text-danger">${frappe.utils.escape_html(errorText)}</div></div>`
			: "",
		`</div>`,
		`</div>`,
		`</div>`,
		`<div class="border rounded p-3">`,
		`<div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">`,
		`<div><strong>${__("Recent Errors and Conflicts")}</strong><div class="text-muted small">${frappe.utils.escape_html(__("Based on the latest {0} loaded run items.", [items.length]))}</div></div>`,
		`<div>${sync.run.renderHealthActions(doc)}</div>`,
		`</div>`,
		sync.run.renderIssueList(doctypeName, issueItems, errorText),
		`</div>`,
	].join("");
};

sync.run.buildHtml = function (frm, doctypeName, items) {
	const count = items.length;
	const header = [
		`<div class="d-flex flex-wrap justify-content-between align-items-center mb-2 gap-2">`,
		`<div><strong>${__("Related Run Items")}</strong>`,
		`<div class="text-muted small">${frappe.utils.escape_html(__("Latest {0} items loaded for quick inspection.", [count]))}</div>`,
		`</div>`,
		doctypeName
			? `<div class="text-muted small">${frappe.utils.escape_html(__("Target DocType: {0}", [doctypeName]))}</div>`
			: `<div class="text-warning small">${frappe.utils.escape_html(__("Target DocType could not be resolved from the Sync Definition."))}</div>`,
		`</div>`,
	];

	if (!count) {
		header.push(`<div class="text-muted">${__("No run items found yet.")}</div>`);
		return header.join("");
	}

	const rows = items
		.map((item) => {
			const documentCell =
				doctypeName && item.document_name
					? `<a href="#" class="sync-run-document-link" data-document-name="${frappe.utils.escape_html(item.document_name)}" data-doctype-name="${frappe.utils.escape_html(doctypeName)}">${frappe.utils.escape_html(item.document_name)}</a>`
					: `<span class="text-muted">${frappe.utils.escape_html(item.document_name || __("Not linked"))}</span>`;
			const recordCell = `<a href="#" class="sync-run-item-link" data-item-name="${frappe.utils.escape_html(item.name)}">${frappe.utils.escape_html(item.record_key || item.name)}</a>`;
			const message = sync.run.truncateText(item.message || "", 120);

			return `
				<tr>
					<td>${recordCell}</td>
					<td>${sync.run.indicator(item.action)}</td>
					<td>${sync.run.indicator(item.status)}</td>
					<td>${documentCell}</td>
					<td>${frappe.utils.escape_html(sync.run.relativeTimestamp(item.creation))}</td>
					<td title="${frappe.utils.escape_html(item.message || "")}">${frappe.utils.escape_html(message)}</td>
				</tr>
			`;
		})
		.join("");

	return [
		...header,
		`<div class="table-responsive">`,
		`<table class="table table-bordered table-hover table-sm mb-0">`,
		`<thead><tr><th>${__("Record")}</th><th>${__("Action")}</th><th>${__("Status")}</th><th>${__("Document")}</th><th>${__("Created")}</th><th>${__("Message")}</th></tr></thead>`,
		`<tbody>${rows}</tbody>`,
		`</table>`,
		`</div>`,
	].join("");
};

sync.run.bindHealthHandlers = function (frm, doctypeName) {
	const wrapper = frm.get_field("run_health_html")?.$wrapper;
	if (!wrapper) {
		return;
	}

	wrapper.off("click", ".sync-run-item-link");
	wrapper.off("click", ".sync-run-document-link");
	wrapper.off("click", ".sync-run-health-filter");

	wrapper.on("click", ".sync-run-item-link", (event) => {
		event.preventDefault();
		const itemName = event.currentTarget.dataset.itemName;
		if (itemName) {
			frappe.set_route("Form", "Sync Run Item", itemName);
		}
	});

	wrapper.on("click", ".sync-run-document-link", (event) => {
		event.preventDefault();
		const linkDoctype = doctypeName || event.currentTarget.dataset.doctypeName;
		const documentName = event.currentTarget.dataset.documentName;
		if (linkDoctype && documentName) {
			frappe.set_route("Form", linkDoctype, documentName);
		}
	});

	wrapper.on("click", ".sync-run-health-filter", (event) => {
		event.preventDefault();
		const status = event.currentTarget.dataset.status;
		if (!status) {
			return;
		}
		frappe.set_route("List", "Sync Run Item", {
			sync_run: frm.doc.name,
			status,
		});
	});
};

sync.run.bindRowHandlers = function (frm, doctypeName) {
	const wrapper = frm.get_field("run_items_html")?.$wrapper;
	if (!wrapper) {
		return;
	}

	wrapper.off("click", ".sync-run-item-link");
	wrapper.off("click", ".sync-run-document-link");

	wrapper.on("click", ".sync-run-item-link", (event) => {
		event.preventDefault();
		const itemName = event.currentTarget.dataset.itemName;
		if (itemName) {
			frappe.set_route("Form", "Sync Run Item", itemName);
		}
	});

	wrapper.on("click", ".sync-run-document-link", (event) => {
		event.preventDefault();
		const linkDoctype = doctypeName || event.currentTarget.dataset.doctypeName;
		const documentName = event.currentTarget.dataset.documentName;
		if (linkDoctype && documentName) {
			frappe.set_route("Form", linkDoctype, documentName);
		}
	});
};

sync.run.truncateText = function (value, maxLength) {
	const text = String(value || "");
	if (text.length <= maxLength) {
		return text;
	}
	return `${text.slice(0, maxLength - 1)}…`;
};

sync.run.indicator = function (value) {
	const text = String(value || __("Unknown"));
	const color = sync.run.indicatorColor(text);
	return `<span class="indicator ${color}">${frappe.utils.escape_html(text)}</span>`;
};

sync.run.indicatorColor = function (value) {
	const key = String(value || "").toLowerCase();
	if (["success", "created", "updated"].includes(key)) {
		return "green";
	}
	if (["error", "partial error", "conflict"].includes(key)) {
		return "red";
	}
	if (["needs review", "skipped"].includes(key)) {
		return "orange";
	}
	if (["running", "queued", "preview"].includes(key)) {
		return "blue";
	}
	return "gray";
};

sync.run.metricCard = function (label, value, color) {
	return [
		`<div class="col-sm-6 col-lg-3 mb-2">`,
		`<div class="border rounded px-2 py-3 text-center h-100">`,
		`<div class="small text-muted">${frappe.utils.escape_html(label)}</div>`,
		`<div class="indicator ${color} justify-content-center" style="font-size: 1.2rem; margin-top: 0.35rem;">${frappe.utils.escape_html(String(value || 0))}</div>`,
		`</div>`,
		`</div>`,
	].join("");
};

sync.run.detailLine = function (label, value) {
	const displayValue = value === null || value === undefined || value === "" ? "" : value;
	return `<div class="mb-1"><span class="text-muted">${frappe.utils.escape_html(label)}:</span> ${frappe.utils.escape_html(String(displayValue))}</div>`;
};

sync.run.isIssueItem = function (item) {
	const status = String(item.status || "").toLowerCase();
	const action = String(item.action || "").toLowerCase();
	return ["error", "conflict", "skipped"].includes(status) || ["error", "conflict"].includes(action);
};

sync.run.renderHealthActions = function (doc) {
	const buttons = [];
	if (cint(doc.error_count)) {
		buttons.push(
			`<a href="#" class="sync-run-health-filter btn btn-xs btn-danger" data-status="error">${frappe.utils.escape_html(__("Errors"))}</a>`
		);
	}
	if (cint(doc.conflict_count)) {
		buttons.push(
			`<a href="#" class="sync-run-health-filter btn btn-xs btn-warning" data-status="conflict">${frappe.utils.escape_html(__("Conflicts"))}</a>`
		);
	}
	return buttons.join(" ");
};

sync.run.renderIssueList = function (doctypeName, issueItems, errorText) {
	if (!issueItems.length) {
		if (!errorText) {
			return `<div class="text-muted">${__("No recent error, conflict or skipped items were found.")}</div>`;
		}
		return `<div class="text-danger">${frappe.utils.escape_html(errorText)}</div>`;
	}

	const rows = issueItems
		.map((item) => {
			const recordLabel = item.record_key || item.name;
			const documentCell =
				doctypeName && item.document_name
					? `<a href="#" class="sync-run-document-link" data-document-name="${frappe.utils.escape_html(item.document_name)}" data-doctype-name="${frappe.utils.escape_html(doctypeName)}">${frappe.utils.escape_html(item.document_name)}</a>`
					: `<span class="text-muted">${frappe.utils.escape_html(item.document_name || __("Not linked"))}</span>`;
			return [
				`<tr>`,
				`<td><a href="#" class="sync-run-item-link" data-item-name="${frappe.utils.escape_html(item.name)}">${frappe.utils.escape_html(recordLabel)}</a></td>`,
				`<td>${sync.run.indicator(item.action || item.status)}</td>`,
				`<td>${sync.run.indicator(item.status)}</td>`,
				`<td>${documentCell}</td>`,
				`<td>${frappe.utils.escape_html(sync.run.relativeTimestamp(item.creation))}</td>`,
				`<td title="${frappe.utils.escape_html(item.message || "")}">${frappe.utils.escape_html(sync.run.truncateText(item.message || "", 110))}</td>`,
				`</tr>`,
			].join("");
		})
		.join("");

	return [
		`<div class="table-responsive">`,
		`<table class="table table-sm table-bordered mb-0">`,
		`<thead><tr><th>${__("Record")}</th><th>${__("Action")}</th><th>${__("Status")}</th><th>${__("Document")}</th><th>${__("Created")}</th><th>${__("Message")}</th></tr></thead>`,
		`<tbody>${rows}</tbody>`,
		`</table>`,
		`</div>`,
	].join("");
};

sync.run.buildCounterSummary = function (doc) {
	return [
		cint(doc.processed_count) ? __("{0} processed", [cint(doc.processed_count)]) : "",
		cint(doc.success_count) ? __("{0} successful", [cint(doc.success_count)]) : "",
		cint(doc.error_count) ? __("{0} errors", [cint(doc.error_count)]) : "",
		cint(doc.conflict_count) ? __("{0} conflicts", [cint(doc.conflict_count)]) : "",
	].filter(Boolean).join(", ");
};

sync.run.relativeTimestamp = function (value) {
	if (!value) {
		return __("Unknown");
	}
	if (frappe.datetime && frappe.datetime.prettyDate) {
		return frappe.datetime.prettyDate(value) || String(value);
	}
	return String(value);
};
