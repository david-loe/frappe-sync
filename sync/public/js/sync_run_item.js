frappe.provide("sync.run_item");

frappe.ui.form.on("Sync Run Item", {
	refresh(frm) {
		sync.run_item.renderDocumentNameLink(frm);
		sync.run_item.setupResolutionButtons(frm);
	},
	sync_definition(frm) {
		sync.run_item.renderDocumentNameLink(frm);
	},
	document_name(frm) {
		sync.run_item.renderDocumentNameLink(frm);
	},
});

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
