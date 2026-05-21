frappe.provide("sync.forms");
sync.helpers = sync.helpers || {};
sync.forms.DEFINITION_PARTNER_COLUMN_METHOD = "get_sync_partner_table_columns";

frappe.ui.form.on("Sync Definition", {
	refresh(frm) {
		sync.helpers.refreshDefinitionFieldPresentation(frm);
		sync.forms.setupButtons(frm);
		sync.helpers.toggleSourceFields(frm);
		sync.helpers.toggleSyncTypeSections(frm);
		sync.helpers.toggleDefinitionModifiedFieldRows(frm);
		sync.helpers.refreshDefinitionSourceValidation(frm);
		sync.helpers.refreshDefinitionFieldChoices(frm);
		sync.helpers.refreshDefinitionPartnerColumnState(frm);
		sync.helpers.setupDefinitionPartnerColumnPanel(frm);
		sync.helpers.refreshDefinitionPartnerColumnChoices(frm);
	},
	partner(frm) {
		sync.helpers.refreshDefinitionPartnerColumnState(frm);
		sync.helpers.setupDefinitionPartnerColumnPanel(frm);
		sync.helpers.refreshDefinitionPartnerColumnChoices(frm);
	},
	sync_type(frm) {
		sync.helpers.refreshDefinitionFieldPresentation(frm);
		sync.helpers.toggleSyncTypeSections(frm);
		sync.helpers.toggleDefinitionModifiedFieldRows(frm);
	},
	doctype_name(frm) {
		sync.helpers.refreshDefinitionFieldChoices(frm);
	},
	read_query(frm) {
		sync.helpers.refreshDefinitionFieldPresentation(frm);
		sync.helpers.toggleSourceFields(frm);
		sync.helpers.refreshDefinitionSourceValidation(frm);
		sync.helpers.refreshDefinitionPartnerColumnState(frm);
		sync.helpers.setupDefinitionPartnerColumnPanel(frm);
		sync.helpers.refreshDefinitionPartnerColumnChoices(frm);
	},
	table_name(frm) {
		sync.helpers.refreshDefinitionFieldPresentation(frm);
		sync.helpers.toggleSourceFields(frm);
		sync.helpers.refreshDefinitionSourceValidation(frm);
		sync.helpers.refreshDefinitionPartnerColumnState(frm);
		sync.helpers.setupDefinitionPartnerColumnPanel(frm);
		sync.helpers.refreshDefinitionPartnerColumnChoices(frm);
	},
	partner_columns(frm) {
		sync.helpers.refreshDefinitionPartnerColumnState(frm);
		sync.helpers.refreshDefinitionPartnerColumnChoices(frm);
		sync.helpers.renderDefinitionPartnerColumnPanel(frm);
	},
	delete_missing(frm) {
		sync.helpers.refreshDefinitionSourceValidation(frm);
	},
	validate(frm) {
		sync.helpers.refreshDefinitionFieldPresentation(frm);
		sync.helpers.validateDefinitionSourceSettings(frm);
	},
});

sync.forms.setupButtons = function (frm) {
	frm.clear_custom_buttons();
	frm.add_custom_button(__("Run Now"), () => {
		sync.helpers.runSyncDefinition(frm, "manual");
	});
	frm.add_custom_button(__("Dry Run"), () => {
		sync.helpers.runSyncDefinition(frm, "manual", { dry_run: true });
	});
	frm.add_custom_button(__("Preview"), () => {
		sync.helpers.previewSyncDefinition(frm);
	});
	frm.add_custom_button(__("Export YAML"), () => {
		sync.helpers.exportDefinitionYaml(frm);
	});
	frm.add_custom_button(__("Import YAML"), () => {
		sync.helpers.importDefinitionYaml(frm);
	});
	frm.add_custom_button(__("Open Latest Run"), () => {
		sync.helpers.openLatestRun(frm);
	});
};

sync.helpers.collectDefinitionFieldChoiceValues = function (frm) {
	const values = [];
	["match_fields", "field_mapping", "value_mapping"].forEach((tableField) => {
		(frm.doc[tableField] || []).forEach((row) => {
			if (row?.frappe_field) {
				values.push(row.frappe_field);
			}
		});
	});
	(frm.doc.frappe_modified_field_rows || []).forEach((row) => {
		if (row?.field_name) {
			values.push(row.field_name);
		}
	});
	if (frm.doc.frappe_partner_identity_field) {
		values.push(frm.doc.frappe_partner_identity_field);
	}
	return values;
};

sync.helpers.applyDefinitionFieldChoices = function (frm, fields) {
	const fieldnames = [
		...new Set([
			...(fields || []).map((field) => field?.fieldname).filter(Boolean),
			...sync.helpers.collectDefinitionFieldChoiceValues(frm),
		]),
	];
	const options = ["", ...fieldnames].join("\n");
	const description = fields?.length
		? __("Field choices loaded from {0}.", [frm.doc.doctype_name])
		: __("Select a DocType to load guided field choices.");

	["match_fields", "field_mapping", "value_mapping"].forEach((tableField) => {
		const grid = frm.fields_dict[tableField]?.grid;
		if (!grid) {
			return;
		}
		grid.update_docfield_property("frappe_field", "options", options);
		grid.update_docfield_property("frappe_field", "description", description);
		frm.refresh_field(tableField);
	});

	frm.set_df_property("frappe_partner_identity_field", "options", options);
	frm.set_df_property("frappe_partner_identity_field", "description", description);
	frm.refresh_field("frappe_partner_identity_field");

	const modifiedFieldsGrid = frm.fields_dict.frappe_modified_field_rows?.grid;
	if (modifiedFieldsGrid) {
		modifiedFieldsGrid.update_docfield_property("field_name", "options", options);
		modifiedFieldsGrid.update_docfield_property("field_name", "description", description);
		frm.refresh_field("frappe_modified_field_rows");
	}
};

sync.helpers.parseDefinitionPartnerColumns = function (rawColumns) {
	if (!rawColumns) {
		return [];
	}
	if (Array.isArray(rawColumns)) {
		return sync.helpers.normalizePartnerColumnChoices(rawColumns);
	}
	if (typeof rawColumns === "object") {
		return sync.helpers.normalizePartnerColumnChoices(rawColumns);
	}

	const raw = String(rawColumns || "").trim();
	if (!raw) {
		return [];
	}

	try {
		return sync.helpers.normalizePartnerColumnChoices(JSON.parse(raw));
	} catch (error) {
		return sync.helpers.normalizePartnerColumnChoices(
			raw
				.split(/\r?\n|,/)
				.map((value) => value.trim())
				.filter(Boolean)
		);
	}
};

sync.helpers.serializeDefinitionPartnerColumns = function (columns) {
	const values = [
		...new Set(
			(Array.isArray(columns) ? columns : [])
				.map((column) => column?.value || column?.label || column)
				.map((value) => String(value || "").trim())
				.filter(Boolean)
		),
	];
	return JSON.stringify(values, null, 2);
};

sync.helpers.getDefinitionPartnerColumnState = function (frm) {
	if (!frm.__sync_partner_columns) {
		frm.__sync_partner_columns = {
			columns: sync.helpers.parseDefinitionPartnerColumns(frm.doc.partner_columns),
			loaded_signature: String(frm.doc.partner_columns_signature || "").trim() || null,
			current_signature: null,
			loaded_at: frm.doc.partner_columns_loaded_at || null,
			loading: false,
			stale: false,
			error: "",
		};
	}
	return frm.__sync_partner_columns;
};

sync.helpers.getDefinitionPartnerColumnSignature = function (frm) {
	return [frm.doc.partner, frm.doc.table_name, sync.helpers.getDefinitionSourceReadQuery(frm)]
		.map((value) => String(value || "").trim())
		.join("::");
};

sync.helpers.isDefinitionPartnerColumnReady = function (frm) {
	return Boolean((frm.doc.partner || "").trim() && (frm.doc.table_name || "").trim());
};

sync.helpers.getDefinitionPartnerColumnRequestArgs = function (frm) {
	const args = {
		sync_partner_name: frm.doc.partner,
		table_name: frm.doc.table_name,
	};
	const readQuery = sync.helpers.getDefinitionSourceReadQuery(frm);
	if (readQuery) {
		args.read_query = readQuery;
	}
	return args;
};

sync.helpers.refreshDefinitionPartnerColumnState = function (frm) {
	const state = sync.helpers.getDefinitionPartnerColumnState(frm);
	const ready = sync.helpers.isDefinitionPartnerColumnReady(frm);
	const signature = ready ? sync.helpers.getDefinitionPartnerColumnSignature(frm) : null;

	state.columns = sync.helpers.parseDefinitionPartnerColumns(frm.doc.partner_columns);
	state.loaded_signature = String(frm.doc.partner_columns_signature || "").trim() || null;
	state.loaded_at = frm.doc.partner_columns_loaded_at || null;
	state.current_signature = signature;
	if (!ready) {
		state.loading = false;
		state.stale = Boolean(state.loaded_signature);
		state.error = "";
		return state;
	}

	if (state.loaded_signature && state.loaded_signature !== signature) {
		state.loading = false;
		state.stale = true;
		state.error = "";
	}

	if (!state.loaded_signature) {
		state.stale = false;
	}

	return state;
};

sync.helpers.applyDefinitionPartnerColumnChoices = function (frm) {
	const state = sync.helpers.getDefinitionPartnerColumnState(frm);
	const columns = Array.isArray(state.columns) ? state.columns : [];
	const partnerFieldOptions = ["", ...new Set(columns.map((column) => column?.value).filter(Boolean))].join("\n");
	const partnerFieldDescription = columns.length
		? __("Partner field choices loaded from the stored partner columns.")
		: __("Load partner columns from {0} to get guided partner-side field selection.", [
				sync.helpers.getDefinitionSourceReadQuery(frm) ? __("Read Query") : __("Table Name"),
		  ]);
	const partnerModifiedDescription = columns.length
		? __("Select partner-side modified fields from the loaded partner source columns.")
		: partnerFieldDescription;

	const fieldMappingGrid = frm.fields_dict.field_mapping?.grid;
	if (fieldMappingGrid) {
		fieldMappingGrid.update_docfield_property("partner_field", "fieldtype", "Select");
		fieldMappingGrid.update_docfield_property("partner_field", "options", partnerFieldOptions);
		fieldMappingGrid.update_docfield_property("partner_field", "ignore_validation", 0);
		fieldMappingGrid.update_docfield_property("partner_field", "description", partnerFieldDescription);
		frm.refresh_field("field_mapping");
	}

	const modifiedFieldsGrid = frm.fields_dict.partner_modified_field_rows?.grid;
	if (modifiedFieldsGrid) {
		modifiedFieldsGrid.update_docfield_property("field_name", "options", partnerFieldOptions);
		modifiedFieldsGrid.update_docfield_property("field_name", "description", partnerModifiedDescription);
		frm.refresh_field("partner_modified_field_rows");
	}

	["partner_identity_field", "partner_frappe_identity_field"].forEach((fieldname) => {
		frm.set_df_property(fieldname, "options", partnerFieldOptions);
		frm.refresh_field(fieldname);
	});
};

sync.helpers.refreshDefinitionPartnerColumnChoices = function (frm) {
	sync.helpers.applyDefinitionPartnerColumnChoices(frm);
};

sync.helpers.renderDefinitionPartnerColumnPanel = function (frm) {
	const field = frm.get_field("partner_columns_status");
	const $wrapper = field?.wrapper ? $(field.wrapper) : null;
	if (!$wrapper?.length) {
		return;
	}

	const state = sync.helpers.getDefinitionPartnerColumnState(frm);
	const missing = [];
	if (!frm.doc.partner) {
		missing.push(__("Partner"));
	}
	if (!frm.doc.table_name) {
		missing.push(__("Table Name"));
	}

	$wrapper.html(
		sync.helpers.renderPartnerColumnStatusPanel(state, {
			ready: sync.helpers.isDefinitionPartnerColumnReady(frm),
			missing,
			source_label: sync.helpers.getDefinitionSourceReadQuery(frm) ? __("Read Query") : __("Table Name"),
			source_details: sync.helpers.getDefinitionSourceReadQuery(frm)
				? __("Partner rows are loaded from the Read Query, while writes still target Table Name.")
				: __("Partner rows are loaded directly from Table Name."),
		})
	);
};

sync.helpers.setupDefinitionPartnerColumnPanel = function (frm) {
	const field = frm.get_field("partner_columns_status");
	const $wrapper = field?.wrapper ? $(field.wrapper) : null;
	if (!$wrapper?.length) {
		return;
	}

	$wrapper.off(".sync-partner-columns");
	$wrapper.on("click.sync-partner-columns", "[data-action='refresh-partner-columns']", (event) => {
		event.preventDefault();
		sync.helpers.loadDefinitionPartnerColumns(frm);
	});
	sync.helpers.renderDefinitionPartnerColumnPanel(frm);
};

sync.helpers.loadDefinitionPartnerColumns = function (frm) {
	if (!sync.helpers.isDefinitionPartnerColumnReady(frm)) {
		frappe.show_alert({
			message: __("Set Partner and Table Name first."),
			indicator: "orange",
		});
		return Promise.resolve([]);
	}

	const state = sync.helpers.getDefinitionPartnerColumnState(frm);
	state.loading = true;
	state.error = "";
	sync.helpers.renderDefinitionPartnerColumnPanel(frm);

	return sync.helpers
		.callApi(sync.forms.DEFINITION_PARTNER_COLUMN_METHOD, sync.helpers.getDefinitionPartnerColumnRequestArgs(frm), {
			freeze: false,
			freeze_message: __("Loading partner columns…"),
		})
		.then((response) => {
			const payload = response?.message || {};
			const columns = sync.helpers.normalizePartnerColumnChoices(payload);
			const signature = sync.helpers.getDefinitionPartnerColumnSignature(frm);
			const loadedAt = payload.loaded_at || payload.refreshed_at || frappe.datetime.now_datetime();
			state.columns = columns;
			state.loaded_signature = signature;
			state.current_signature = state.loaded_signature;
			state.loaded_at = loadedAt;
			state.loading = false;
			state.stale = false;
			state.error = "";
			frm.set_value("partner_columns", sync.helpers.serializeDefinitionPartnerColumns(columns));
			frm.set_value("partner_columns_signature", signature);
			frm.set_value("partner_columns_loaded_at", loadedAt);
			sync.helpers.applyDefinitionPartnerColumnChoices(frm);
			sync.helpers.renderDefinitionPartnerColumnPanel(frm);
			frappe.show_alert({
				message: __("Loaded {0} partner columns.", [columns.length]),
				indicator: "green",
			});
			return columns;
		})
		.catch((error) => {
			state.loading = false;
			state.stale = true;
			state.error = sync.helpers.isMissingApiMethodError(error)
				? __("Partner-column loading is not available yet. Partner field selections require stored partner columns.")
				: sync.helpers.extractApiErrorMessage(error);
			sync.helpers.applyDefinitionPartnerColumnChoices(frm);
			sync.helpers.renderDefinitionPartnerColumnPanel(frm);
			frappe.show_alert({
				message: state.error,
				indicator: "orange",
			});
			return [];
		});
};

sync.helpers.toggleDefinitionModifiedFieldRows = function (frm) {
	const direction = (frm.doc.sync_type || "").toLowerCase();
	const frappeVisible = direction !== "a<-b";
	const partnerVisible = direction !== "a->b";
	frm.toggle_display("frappe_modified_field_rows", frappeVisible);
	frm.toggle_display("partner_modified_field_rows", partnerVisible);
	frm.set_df_property(
		"frappe_modified_field_rows",
		"description",
		frappeVisible ? __("Fields used to detect changes on the Frappe side.") : ""
	);
	frm.set_df_property(
		"partner_modified_field_rows",
		"description",
		partnerVisible ? __("Fields used to detect changes on the partner side.") : ""
	);
};

sync.helpers.refreshDefinitionFieldChoices = function (frm) {
	const doctypeName = frm.doc.doctype_name;
	if (!doctypeName) {
		sync.helpers.applyDefinitionFieldChoices(frm, []);
		return Promise.resolve([]);
	}

	return sync.helpers
		.callApi(
			"get_sync_definition_field_choices",
			{ doctype_name: doctypeName },
			{ freeze: false }
		)
		.then((response) => {
			const payload = response?.message || {};
			const fields = Array.isArray(payload.fields) ? payload.fields : [];
			sync.helpers.applyDefinitionFieldChoices(frm, fields);
			return fields;
		})
		.catch((error) => {
			sync.helpers.applyDefinitionFieldChoices(frm, []);
			frappe.show_alert({
				message: error?.message || __("Unable to load field choices."),
				indicator: "red",
			});
			return [];
		});
};

sync.helpers.getDefinitionSourceSettingsIssue = function (frm) {
	const hasTableName = Boolean((frm.doc.table_name || "").trim());
	const hasQuery = Boolean(sync.helpers.getDefinitionSourceReadQuery(frm));
	if (!hasTableName) {
		return __("Table Name is required.");
	}
	if (frm.doc.delete_missing && hasQuery) {
		return __("Delete Missing cannot be enabled when Read Query is used.");
	}
	return null;
};

sync.helpers.getDefinitionSourceSettingsNotice = function (frm) {
	if (!sync.helpers.getDefinitionSourceReadQuery(frm)) {
		return "";
	}
	return __("Read Query is active. Partner rows are loaded from the query, while inserts and updates still target Table Name.");
};

sync.helpers.refreshDefinitionSourceValidation = function (frm) {
	const issue = sync.helpers.getDefinitionSourceSettingsIssue(frm);
	if (issue) {
		frm.set_intro(issue, "orange");
		return;
	}
	const notice = sync.helpers.getDefinitionSourceSettingsNotice(frm);
	frm.set_intro(notice, notice ? "blue" : "");
};

sync.helpers.validateDefinitionSourceSettings = function (frm) {
	const issue = sync.helpers.getDefinitionSourceSettingsIssue(frm);
	if (issue) {
		frappe.throw(issue);
	}
};
