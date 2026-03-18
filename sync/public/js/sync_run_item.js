frappe.provide("sync.run_item");

frappe.ui.form.on("Sync Run Item", {
	refresh(frm) {
		frm.__sync_run_item_button_render_id = (frm.__sync_run_item_button_render_id || 0) + 1;
		sync.run_item.setupButtons(frm);
		sync.run_item.renderSummary(frm);
	},
	sync_definition(frm) {
		sync.run_item.clearCache(frm);
		frm.__sync_run_item_button_render_id = (frm.__sync_run_item_button_render_id || 0) + 1;
		sync.run_item.setupButtons(frm);
		sync.run_item.renderSummary(frm);
	},
});

sync.run_item.clearCache = function (frm) {
	frm.__sync_run_item_summary_render_id = (frm.__sync_run_item_summary_render_id || 0) + 1;
};

sync.run_item.setupButtons = function (frm) {
	frm.clear_custom_buttons();

	if (frm.doc.sync_run) {
		frm.add_custom_button(__("Open Parent Run"), () => {
			frappe.set_route("Form", "Sync Run", frm.doc.sync_run);
		});
	}

	if (!frm.doc.document_name) {
		return;
	}

	const renderId = frm.__sync_run_item_button_render_id || 0;
	sync.run_item.getTargetDoctype(frm).then((doctypeName) => {
		if (renderId !== frm.__sync_run_item_button_render_id) {
			return;
		}
		if (!doctypeName) {
			return;
		}
		frm.add_custom_button(__("Open Related Document"), () => {
			frappe.set_route("Form", doctypeName, frm.doc.document_name);
		});
	});
};

sync.run_item.getTargetDoctype = function (frm) {
	return sync.helpers.getSyncDefinitionDoctype(frm.doc.sync_definition);
};

sync.run_item.renderSummary = function (frm) {
	const field = frm.get_field("monitoring_html");
	if (!field || !field.$wrapper) {
		return;
	}

	if (frm.is_new()) {
		field.$wrapper.html(`<div class="text-muted">${__("Save the run item to load monitoring details.")}</div>`);
		return;
	}

	const renderId = (frm.__sync_run_item_summary_render_id || 0) + 1;
	frm.__sync_run_item_summary_render_id = renderId;
	field.$wrapper.html(`<div class="text-muted">${__("Loading run item summary…")}</div>`);

	sync.run_item
		.getTargetDoctype(frm)
		.then((doctypeName) => {
			if (renderId !== frm.__sync_run_item_summary_render_id) {
				return;
			}
			field.$wrapper.html(sync.run_item.buildSummaryHtml(frm, doctypeName));
			sync.run_item.bindSummaryHandlers(frm, doctypeName);
		})
		.catch((error) => {
			if (renderId !== frm.__sync_run_item_summary_render_id) {
				return;
			}
			field.$wrapper.html(
				`<div class="text-danger">${frappe.utils.escape_html(error?.message || __("Unable to load run item summary."))}</div>`
			);
		});
};

sync.run_item.buildSummaryHtml = function (frm, doctypeName) {
	const doc = frm.doc;
	const tone = sync.run_item.indicatorColor(doc.action, doc.status);
	const messageTone = ["error", "conflict"].includes(String(doc.status || "").toLowerCase()) ? "text-danger" : "text-muted";
	const frappePayload = sync.run_item.inspectPayload(doc.frappe_payload);
	const partnerPayload = sync.run_item.inspectPayload(doc.partner_payload);

	return [
		`<div class="row">`,
		`<div class="col-sm-7 mb-3">`,
		`<div class="border rounded p-3 h-100">`,
		`<div class="d-flex flex-wrap justify-content-between align-items-start gap-2 mb-2">`,
		`<div><div class="text-muted small">${__("Item Health")}</div><div><span class="indicator ${tone}">${frappe.utils.escape_html(sync.run_item.buildStatusLabel(doc))}</span></div></div>`,
		doc.sync_run
			? `<a href="#" class="sync-run-item-parent-link small" data-sync-run="${frappe.utils.escape_html(doc.sync_run)}">${frappe.utils.escape_html(__("Open run {0}", [doc.sync_run]))}</a>`
			: "",
		`</div>`,
		`<div class="${messageTone}">${frappe.utils.escape_html(doc.message || __("No operator message recorded."))}</div>`,
		`<div class="mt-3">`,
		sync.run_item.detailLine(__("Record Key"), doc.record_key || __("Missing")),
		sync.run_item.detailLine(__("Direction"), doc.direction || __("Unknown")),
		sync.run_item.detailLine(__("Source ID"), doc.source_id || __("Not recorded")),
		sync.run_item.detailLine(__("Target ID"), doc.target_id || __("Not recorded")),
		`</div>`,
		`</div>`,
		`</div>`,
		`<div class="col-sm-5 mb-3">`,
		`<div class="border rounded p-3 h-100">`,
		`<div class="text-muted small mb-2">${__("Links and Targets")}</div>`,
		sync.run_item.renderDocumentLink(doctypeName, doc.document_name),
		sync.run_item.detailLine(__("Sync Definition"), doc.sync_definition || __("Missing")),
		doctypeName
			? sync.run_item.detailLine(__("Target DocType"), doctypeName)
			: `<div class="text-warning small">${frappe.utils.escape_html(__("Target DocType could not be resolved from the Sync Definition."))}</div>`,
		`</div>`,
		`</div>`,
		`</div>`,
		`<div class="row">`,
		`<div class="col-sm-6 mb-3">`,
		sync.run_item.renderPayloadCard(__("Frappe Payload"), frappePayload),
		`</div>`,
		`<div class="col-sm-6 mb-3">`,
		sync.run_item.renderPayloadCard(__("Partner Payload"), partnerPayload),
		`</div>`,
		`</div>`,
	].join("");
};

sync.run_item.bindSummaryHandlers = function (frm, doctypeName) {
	const wrapper = frm.get_field("monitoring_html")?.$wrapper;
	if (!wrapper) {
		return;
	}

	wrapper.off("click", ".sync-run-item-parent-link");
	wrapper.off("click", ".sync-run-item-document-link");

	wrapper.on("click", ".sync-run-item-parent-link", (event) => {
		event.preventDefault();
		const runName = event.currentTarget.dataset.syncRun;
		if (runName) {
			frappe.set_route("Form", "Sync Run", runName);
		}
	});

	wrapper.on("click", ".sync-run-item-document-link", (event) => {
		event.preventDefault();
		const targetDoctype = doctypeName || event.currentTarget.dataset.doctypeName;
		const documentName = event.currentTarget.dataset.documentName;
		if (targetDoctype && documentName) {
			frappe.set_route("Form", targetDoctype, documentName);
		}
	});
};

sync.run_item.renderDocumentLink = function (doctypeName, documentName) {
	if (!documentName) {
		return `<div class="text-muted">${__("No related document linked.")}</div>`;
	}
	if (!doctypeName) {
		return sync.run_item.detailLine(__("Document Name"), documentName);
	}
	return `<div class="mb-2"><a href="#" class="sync-run-item-document-link" data-doctype-name="${frappe.utils.escape_html(doctypeName)}" data-document-name="${frappe.utils.escape_html(documentName)}">${frappe.utils.escape_html(__("Open related document {0}", [documentName]))}</a></div>`;
};

sync.run_item.renderPayloadCard = function (label, payloadInfo) {
	return [
		`<div class="border rounded p-3 h-100">`,
		`<div class="text-muted small mb-2">${frappe.utils.escape_html(label)}</div>`,
		payloadInfo.empty
			? `<div class="text-muted">${__("No payload captured.")}</div>`
			: [
				sync.run_item.detailLine(__("Shape"), payloadInfo.shape),
				sync.run_item.detailLine(__("Approx. Size"), payloadInfo.sizeLabel),
				payloadInfo.keyPreview ? sync.run_item.detailLine(__("Top-level Keys"), payloadInfo.keyPreview) : "",
			].join(""),
		`</div>`,
	].join("");
};

sync.run_item.inspectPayload = function (value) {
	const text = String(value || "").trim();
	if (!text) {
		return { empty: true };
	}

	const sizeLabel = __("{0} chars", [text.length]);
	try {
		const parsed = JSON.parse(text);
		if (Array.isArray(parsed)) {
			return {
				empty: false,
				shape: __("Array ({0})", [parsed.length]),
				sizeLabel,
				keyPreview: "",
			};
		}
		if (parsed && typeof parsed === "object") {
			const keys = Object.keys(parsed);
			return {
				empty: false,
				shape: __("Object ({0} keys)", [keys.length]),
				sizeLabel,
				keyPreview: keys.slice(0, 6).join(", "),
			};
		}
		return {
			empty: false,
			shape: __(typeof parsed),
			sizeLabel,
			keyPreview: "",
		};
	} catch (error) {
		return {
			empty: false,
			shape: __("Text"),
			sizeLabel,
			keyPreview: sync.run_item.truncate(text.replace(/\s+/g, " "), 80),
		};
	}
};

sync.run_item.buildStatusLabel = function (doc) {
	return [doc.action, doc.status].filter(Boolean).join(" / ") || __("Unknown");
};

sync.run_item.indicatorColor = function (action, status) {
	const actionKey = String(action || "").toLowerCase();
	const statusKey = String(status || "").toLowerCase();
	if (["error", "conflict"].includes(statusKey) || ["error", "conflict"].includes(actionKey)) {
		return "red";
	}
	if (statusKey === "success") {
		return "green";
	}
	if (statusKey === "skipped" || actionKey === "skipped") {
		return "orange";
	}
	return "blue";
};

sync.run_item.detailLine = function (label, value) {
	return `<div class="mb-1"><span class="text-muted">${frappe.utils.escape_html(label)}:</span> ${frappe.utils.escape_html(String(value || ""))}</div>`;
};

sync.run_item.truncate = function (value, maxLength) {
	const text = String(value || "");
	if (text.length <= maxLength) {
		return text;
	}
	return `${text.slice(0, maxLength - 1)}…`;
};
