frappe.provide("sync.run_item");

frappe.ui.form.on("Sync Run Item", {
	refresh(frm) {
		sync.run_item.renderDocumentNameLink(frm);
		sync.run_item.renderResolutionPreview(frm);
		sync.run_item.setupResolutionButtons(frm);
	},
	sync_definition(frm) {
		sync.run_item.renderDocumentNameLink(frm);
	},
	document_name(frm) {
		sync.run_item.renderDocumentNameLink(frm);
	},
});

sync.run_item.renderResolutionPreview = function (frm) {
	const hasResolutionPayload = Boolean(frm.doc?.frappe_resolution_payload || frm.doc?.partner_resolution_payload);
	frm.set_df_property("section_break_resolution", "hidden", hasResolutionPayload ? 0 : 1);
	frm.set_df_property("resolution_preview", "hidden", hasResolutionPayload ? 0 : 1);

	const field = frm.get_field("resolution_preview");
	if (!field?.$wrapper) {
		return;
	}

	if (!hasResolutionPayload) {
		field.$wrapper.html("");
		return;
	}

	const renderId = (frm.__sync_run_item_resolution_render_id || 0) + 1;
	frm.__sync_run_item_resolution_render_id = renderId;
	field.$wrapper.html(`<div class="text-muted">${frappe.utils.escape_html(__("Loading resolution mapping..."))}</div>`);

	sync.run_item
		.getSyncDefinitionMapping(frm.doc.sync_definition)
		.then((mapping) => {
			if (renderId !== frm.__sync_run_item_resolution_render_id) {
				return;
			}
			field.$wrapper.html(sync.run_item.buildResolutionPreviewHtml(frm.doc || {}, mapping));
		})
		.catch((error) => {
			if (renderId !== frm.__sync_run_item_resolution_render_id) {
				return;
			}
			const message = error?.message || __("Sync Definition field mapping could not be loaded.");
			field.$wrapper.html(`<div class="text-warning">${frappe.utils.escape_html(message)}</div>`);
		});
};

sync.run_item.buildResolutionPreviewHtml = function (doc, mapping) {
	const payloads = {
		frappeBefore: sync.run_item.parsePayload(doc.frappe_before_payload),
		partnerBefore: sync.run_item.parsePayload(doc.partner_before_payload),
		frappeResolution: sync.run_item.parsePayload(doc.frappe_resolution_payload),
		partnerResolution: sync.run_item.parsePayload(doc.partner_resolution_payload),
	};
	const parseErrors = Object.values(payloads)
		.filter((entry) => entry.error)
		.map((entry) => entry.error);

	if (!payloads.frappeResolution.value && !payloads.partnerResolution.value) {
		return `<div class="text-muted">${frappe.utils.escape_html(__("No manual resolution payloads are available for this Sync Run Item."))}</div>`;
	}

	const fields = sync.run_item.getResolutionFields(doc.changed_fields, payloads, mapping);
	if (!fields.length) {
		return `<div class="text-muted">${frappe.utils.escape_html(__("No mapped resolution fields are available."))}</div>`;
	}

	const changedFields = new Set(sync.run_item.parseChangedFields(doc.changed_fields));
	const rows = fields
		.map((field) => {
			const frappeBefore = sync.run_item.getPayloadValue(payloads.frappeBefore.value, field.frappe_field);
			const partnerBefore = sync.run_item.getPayloadValue(payloads.partnerBefore.value, field.partner_field);
			const frappeResolution = sync.run_item.getPayloadValue(payloads.frappeResolution.value, field.frappe_field);
			const partnerResolution = sync.run_item.getPayloadValue(payloads.partnerResolution.value, field.partner_field);
			const rowHasChange = sync.run_item.hasResolutionChange(field, changedFields);
			return `
				<tr class="${rowHasChange ? "table-warning" : ""}">
					<th class="text-muted">${frappe.utils.escape_html(field.frappe_field)}</th>
					<td>${sync.run_item.renderResolutionValue(frappeBefore)}</td>
					<td>${sync.run_item.renderResolutionValue(partnerBefore)}</td>
					<td>${sync.run_item.renderResolutionValue(frappeResolution)}</td>
					<td>${sync.run_item.renderResolutionValue(partnerResolution)}</td>
				</tr>
			`;
		})
		.join("");

	const errorHtml = parseErrors.length
		? `<div class="text-warning small mb-2">${frappe.utils.escape_html(__("Some payloads could not be parsed as JSON and are shown as raw text."))}</div>`
		: "";

	return `
		<div class="sync-run-item-resolution-preview">
			${errorHtml}
			<div class="table-responsive" style="max-height: 420px; overflow: auto;">
				<table class="table table-bordered table-sm mb-0">
					<thead>
						<tr>
							<th style="min-width: 160px;">${frappe.utils.escape_html(__("Field"))}</th>
							<th>${frappe.utils.escape_html(__("Current Frappe"))}</th>
							<th>${frappe.utils.escape_html(__("Current Partner"))}</th>
							<th>${frappe.utils.escape_html(__("Write to Frappe"))}</th>
							<th>${frappe.utils.escape_html(__("Write to Partner"))}</th>
						</tr>
					</thead>
					<tbody>${rows}</tbody>
				</table>
			</div>
		</div>
	`;
};

sync.run_item.getSyncDefinitionMapping = function (syncDefinitionName) {
	const name = String(syncDefinitionName || "").trim();
	if (!name) {
		return Promise.resolve([]);
	}

	sync.run_item._sync_definition_mapping_cache = sync.run_item._sync_definition_mapping_cache || {};
	sync.run_item._sync_definition_mapping_promises = sync.run_item._sync_definition_mapping_promises || {};

	if (Object.prototype.hasOwnProperty.call(sync.run_item._sync_definition_mapping_cache, name)) {
		return Promise.resolve(sync.run_item._sync_definition_mapping_cache[name]);
	}

	if (sync.run_item._sync_definition_mapping_promises[name]) {
		return sync.run_item._sync_definition_mapping_promises[name];
	}

	const request = Promise.resolve()
		.then(() => frappe.db.get_doc("Sync Definition", name))
		.then((doc) => {
			const mapping = sync.run_item.normalizeResolutionMapping(doc?.field_mapping || [], doc || {});
			sync.run_item._sync_definition_mapping_cache[name] = mapping;
			return mapping;
		})
		.finally(() => {
			delete sync.run_item._sync_definition_mapping_promises[name];
		});

	sync.run_item._sync_definition_mapping_promises[name] = request;
	return request;
};

sync.run_item.normalizeResolutionMapping = function (fieldMapping, syncDefinition) {
	const seen = new Set();
	const rows = (Array.isArray(fieldMapping) ? fieldMapping : [])
		.map((row) => ({
			frappe_field: String(row?.frappe_field || "").trim(),
			partner_field: String(row?.partner_field || "").trim(),
		}))
		.filter((row) => row.frappe_field && row.partner_field)
		.filter((row) => {
			if (seen.has(row.frappe_field)) {
				return false;
			}
			seen.add(row.frappe_field);
			return true;
		});

	const frappeModifiedField = String(syncDefinition?.frappe_modified_field || "").trim();
	const partnerModifiedField = String(syncDefinition?.partner_modified_field || "").trim();
	if (frappeModifiedField && partnerModifiedField && !seen.has(frappeModifiedField)) {
		rows.push({
			frappe_field: frappeModifiedField,
			partner_field: partnerModifiedField,
			always_show: true,
		});
	}

	return rows;
};

sync.run_item.parsePayload = function (payload) {
	if (payload === undefined || payload === null || payload === "") {
		return { value: null };
	}
	if (typeof payload === "object") {
		return { value: payload };
	}
	try {
		return { value: JSON.parse(String(payload)) };
	} catch (error) {
		return { value: String(payload), error };
	}
};

sync.run_item.getResolutionFields = function (changedFields, payloads, mapping) {
	const fields = [];
	const seen = new Set();
	const changed = new Set(sync.run_item.parseChangedFields(changedFields));
	const addField = (field) => {
		if (!field?.frappe_field || !field?.partner_field || seen.has(field.frappe_field)) {
			return;
		}
		if (
			!field.always_show &&
			!sync.run_item.hasResolutionFieldValue(field, payloads) &&
			!changed.has(field.frappe_field) &&
			!changed.has(field.partner_field)
		) {
			return;
		}
		seen.add(field.frappe_field);
		fields.push(field);
	};

	(Array.isArray(mapping) ? mapping : []).forEach(addField);

	return fields;
};

sync.run_item.hasResolutionFieldValue = function (field, payloads) {
	return (
		sync.run_item.hasPayloadField(payloads.frappeBefore.value, field.frappe_field) ||
		sync.run_item.hasPayloadField(payloads.partnerBefore.value, field.partner_field) ||
		sync.run_item.hasPayloadField(payloads.frappeResolution.value, field.frappe_field) ||
		sync.run_item.hasPayloadField(payloads.partnerResolution.value, field.partner_field)
	);
};

sync.run_item.hasResolutionChange = function (field, changedFields) {
	return Boolean(
		changedFields && field && (changedFields.has(field.frappe_field) || changedFields.has(field.partner_field))
	);
};

sync.run_item.hasPayloadField = function (payload, fieldname) {
	return Boolean(
		payload &&
			typeof payload === "object" &&
			!Array.isArray(payload) &&
			Object.prototype.hasOwnProperty.call(payload, fieldname)
	);
};

sync.run_item.parseChangedFields = function (changedFields) {
	return String(changedFields || "")
		.split(/[\n,]+/)
		.map((fieldname) => fieldname.trim())
		.filter(Boolean);
};

sync.run_item.getPayloadValue = function (payload, fieldname) {
	if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
		return undefined;
	}
	return Object.prototype.hasOwnProperty.call(payload, fieldname) ? payload[fieldname] : undefined;
};

sync.run_item.renderResolutionValue = function (value) {
	if (value === undefined || value === null || value === "") {
		return `<span class="text-muted">${frappe.utils.escape_html(__("None"))}</span>`;
	}
	if (typeof value === "object") {
		return `<pre class="mb-0" style="white-space: pre-wrap;">${frappe.utils.escape_html(JSON.stringify(value, null, 2))}</pre>`;
	}
	return frappe.utils.escape_html(String(value));
};

sync.run_item.setupResolutionButtons = function (frm) {
	if (frm.is_new() || frm.doc.status !== "conflict" || frm.doc.action !== "conflict" || frm.doc.write_direction) {
		return;
	}

	sync.run_item.addResolutionButton(frm, __("Accept Frappe Changes"), "Frappe -> Partner");
	sync.run_item.addResolutionButton(frm, __("Accept Partner Changes"), "Frappe <- Partner");
};

sync.run_item.addResolutionButton = function (frm, label, direction) {
	frm.add_custom_button(label, () => {
		frappe.confirm(
			__("This will write the selected changes for this Sync Run Item. Continue?"),
			() => sync.run_item.resolve(frm, direction)
		);
	});
};

sync.run_item.resolve = function (frm, direction) {
	return frappe
		.call({
			method: "sync.api.resolve_sync_run_item",
			args: {
				sync_run_item_name: frm.doc.name,
				direction,
			},
			freeze: true,
			freeze_message: __("Resolving Sync Run Item..."),
		})
		.then((response) => {
			const result = response?.message || {};
			if (result.ok) {
				frappe.show_alert({ message: __("Sync Run Item resolved."), indicator: "green" });
			}
			return frm.reload_doc();
		});
};

sync.run_item.renderDocumentNameLink = function (frm) {
	if (!frm.doc.document_name) {
		sync.run_item.setDocumentNameDescription(frm, "");
		return;
	}

	const renderId = (frm.__sync_run_item_document_link_render_id || 0) + 1;
	frm.__sync_run_item_document_link_render_id = renderId;

	sync.helpers
		.getSyncDefinitionDoctype(frm.doc.sync_definition)
		.then((doctypeName) => {
			if (renderId !== frm.__sync_run_item_document_link_render_id) {
				return;
			}
			if (!doctypeName) {
				sync.run_item.setDocumentNameDescription(
					frm,
					`<span class="text-warning">${frappe.utils.escape_html(__("Target DocType could not be resolved from the Sync Definition."))}</span>`
				);
				return;
			}
			const doctype = frappe.utils.escape_html(doctypeName);
			const documentName = frappe.utils.escape_html(frm.doc.document_name);
			sync.run_item.setDocumentNameDescription(
				frm,
				`<a href="#" class="sync-run-item-document-link" data-doctype-name="${doctype}" data-document-name="${documentName}">${frappe.utils.escape_html(__("Open related document {0}", [frm.doc.document_name]))}</a>`
			);
			sync.run_item.bindDocumentNameLink(frm);
		})
		.catch((error) => {
			if (renderId !== frm.__sync_run_item_document_link_render_id) {
				return;
			}
			sync.run_item.setDocumentNameDescription(
				frm,
				`<span class="text-danger">${frappe.utils.escape_html(error?.message || __("Unable to resolve target DocType."))}</span>`
			);
		});
};

sync.run_item.setDocumentNameDescription = function (frm, html) {
	frm.set_df_property("document_name", "description", html || "");
	frm.refresh_field("document_name");
};

sync.run_item.bindDocumentNameLink = function (frm) {
	const wrapper = frm.get_field("document_name")?.$wrapper;
	if (!wrapper) {
		return;
	}
	wrapper.off("click", ".sync-run-item-document-link");
	wrapper.on("click", ".sync-run-item-document-link", (event) => {
		event.preventDefault();
		const doctypeName = event.currentTarget.dataset.doctypeName;
		const documentName = event.currentTarget.dataset.documentName;
		if (doctypeName && documentName) {
			frappe.set_route("Form", doctypeName, documentName);
		}
	});
};
