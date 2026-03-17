frappe.provide("sync.forms");
sync.helpers = sync.helpers || {};
sync.forms.DEFINITION_PARTNER_COLUMN_METHODS = [
	"get_sync_partner_table_columns",
	"load_sync_partner_table_columns",
	"preview_sync_partner_table_columns",
];

frappe.ui.form.on("Sync Definition", {
	refresh(frm) {
		sync.forms.setupButtons(frm);
		sync.helpers.ensureModifiedFieldRowsFromLegacy(frm);
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
		sync.helpers.toggleSyncTypeSections(frm);
		sync.helpers.toggleDefinitionModifiedFieldRows(frm);
	},
	doctype_name(frm) {
		sync.helpers.ensureModifiedFieldRowsFromLegacy(frm);
		sync.helpers.refreshDefinitionFieldChoices(frm);
	},
	query(frm) {
		sync.helpers.toggleSourceFields(frm);
		sync.helpers.refreshDefinitionSourceValidation(frm);
		sync.helpers.refreshDefinitionPartnerColumnState(frm);
		sync.helpers.setupDefinitionPartnerColumnPanel(frm);
		sync.helpers.refreshDefinitionPartnerColumnChoices(frm);
	},
	table_name(frm) {
		sync.helpers.toggleSourceFields(frm);
		sync.helpers.refreshDefinitionSourceValidation(frm);
		sync.helpers.refreshDefinitionPartnerColumnState(frm);
		sync.helpers.setupDefinitionPartnerColumnPanel(frm);
		sync.helpers.refreshDefinitionPartnerColumnChoices(frm);
	},
	delete_missing(frm) {
		sync.helpers.refreshDefinitionSourceValidation(frm);
	},
	validate(frm) {
		sync.helpers.ensureModifiedFieldRowsFromLegacy(frm);
		sync.helpers.validateDefinitionSourceSettings(frm);
	},
});

sync.forms.setupButtons = function (frm) {
	frm.clear_custom_buttons();
	frm.add_custom_button(__("Run Now"), () => {
		sync.helpers.runSyncDefinition(frm, "manual");
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
	["key_fields", "field_mapping", "value_mapping"].forEach((tableField) => {
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

	["key_fields", "field_mapping", "value_mapping"].forEach((tableField) => {
		const grid = frm.fields_dict[tableField]?.grid;
		if (!grid) {
			return;
		}
		grid.update_docfield_property("frappe_field", "options", options);
		grid.update_docfield_property("frappe_field", "description", description);
		frm.refresh_field(tableField);
	});

	const modifiedFieldsGrid = frm.fields_dict.frappe_modified_field_rows?.grid;
	if (!modifiedFieldsGrid) {
		return;
	}
	modifiedFieldsGrid.update_docfield_property("field_name", "options", options);
	modifiedFieldsGrid.update_docfield_property("field_name", "description", description);
	frm.refresh_field("frappe_modified_field_rows");
};

sync.helpers.collectDefinitionPartnerFieldValues = function (frm) {
	const values = [];
	(frm.doc.field_mapping || []).forEach((row) => {
		if (row?.partner_field) {
			values.push(row.partner_field);
		}
	});
	(frm.doc.partner_modified_field_rows || []).forEach((row) => {
		if (row?.field_name) {
			values.push(row.field_name);
		}
	});
	return [...new Set(values.filter(Boolean))];
};

sync.helpers.getDefinitionPartnerColumnState = function (frm) {
	if (!frm.__sync_partner_columns) {
		frm.__sync_partner_columns = {
			columns: [],
			loaded_signature: null,
			current_signature: null,
			loaded_at: null,
			loading: false,
			stale: false,
			error: "",
		};
	}
	return frm.__sync_partner_columns;
};

sync.helpers.getDefinitionPartnerColumnSignature = function (frm) {
	return [frm.doc.partner, frm.doc.table_name].map((value) => String(value || "").trim()).join("::");
};

sync.helpers.isDefinitionPartnerColumnReady = function (frm) {
	return Boolean((frm.doc.partner || "").trim() && (frm.doc.table_name || "").trim() && !String(frm.doc.query || "").trim());
};

sync.helpers.getDefinitionPartnerColumnRequestArgs = function (frm) {
	return {
		sync_partner_name: frm.doc.partner,
		partner: frm.doc.partner,
		table_name: frm.doc.table_name,
	};
};

sync.helpers.refreshDefinitionPartnerColumnState = function (frm) {
	const state = sync.helpers.getDefinitionPartnerColumnState(frm);
	const ready = sync.helpers.isDefinitionPartnerColumnReady(frm);
	const signature = ready ? sync.helpers.getDefinitionPartnerColumnSignature(frm) : null;

	state.current_signature = signature;
	if (!ready) {
		state.columns = [];
		state.loaded_signature = null;
		state.loaded_at = null;
		state.loading = false;
		state.stale = false;
		state.error = "";
		return state;
	}

	if (state.loaded_signature && state.loaded_signature !== signature) {
		state.columns = [];
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
	const currentValues = sync.helpers.collectDefinitionPartnerFieldValues(frm);
	const augmentedColumns = [
		...columns,
		...currentValues
			.filter((value) => !columns.some((column) => column?.value === value))
			.map((value) => ({ label: value, value })),
	];
	const autocompleteOptions = JSON.stringify(augmentedColumns);
	const modifiedFieldOptions = ["", ...new Set(augmentedColumns.map((column) => column?.value).filter(Boolean))].join("\n");
	const partnerFieldDescription = columns.length
		? __("Autocomplete suggestions loaded from the partner table columns.")
		: __("Load partner columns from the Source section to get mapping suggestions.");
	const partnerModifiedDescription = columns.length
		? __("Select partner-side modified fields from the loaded partner columns.")
		: __("Load partner columns from the Source section to guide partner-side modified fields.");

	const fieldMappingGrid = frm.fields_dict.field_mapping?.grid;
	if (fieldMappingGrid) {
		fieldMappingGrid.update_docfield_property("partner_field", "fieldtype", columns.length ? "Autocomplete" : "Data");
		fieldMappingGrid.update_docfield_property("partner_field", "options", columns.length ? autocompleteOptions : "");
		fieldMappingGrid.update_docfield_property("partner_field", "ignore_validation", 1);
		fieldMappingGrid.update_docfield_property("partner_field", "description", partnerFieldDescription);
		frm.refresh_field("field_mapping");
	}

	const modifiedFieldsGrid = frm.fields_dict.partner_modified_field_rows?.grid;
	if (modifiedFieldsGrid) {
		modifiedFieldsGrid.update_docfield_property("field_name", "options", modifiedFieldOptions);
		modifiedFieldsGrid.update_docfield_property("field_name", "description", partnerModifiedDescription);
		frm.refresh_field("partner_modified_field_rows");
	}
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
	if (frm.doc.query) {
		missing.push(__("Table Name instead of Query"));
	} else if (!frm.doc.table_name) {
		missing.push(__("Table Name"));
	}

	$wrapper.html(
		sync.helpers.renderPartnerColumnStatusPanel(state, {
			ready: sync.helpers.isDefinitionPartnerColumnReady(frm),
			missing,
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
		.callFirstAvailableApi(
			sync.forms.DEFINITION_PARTNER_COLUMN_METHODS,
			sync.helpers.getDefinitionPartnerColumnRequestArgs(frm),
			{ freeze: false, freeze_message: __("Loading partner columns…") }
		)
		.then((response) => {
			const payload = response?.message || {};
			const columns = sync.helpers.normalizePartnerColumnChoices(payload);
			state.columns = columns;
			state.loaded_signature = sync.helpers.getDefinitionPartnerColumnSignature(frm);
			state.current_signature = state.loaded_signature;
			state.loaded_at = payload.loaded_at || payload.refreshed_at || frappe.datetime.now_datetime();
			state.loading = false;
			state.stale = false;
			state.error = "";
			sync.helpers.applyDefinitionPartnerColumnChoices(frm);
			sync.helpers.renderDefinitionPartnerColumnPanel(frm);
			frappe.show_alert({
				message: __("Loaded {0} partner columns.", [columns.length]),
				indicator: "green",
			});
			return columns;
		})
		.catch((error) => {
			state.columns = [];
			state.loading = false;
			state.stale = true;
			state.error = sync.helpers.isMissingApiMethodError(error)
				? __("Partner-column loading is not available yet. You can continue with manual partner field names.")
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

sync.helpers.parseDefinitionFieldLines = function (value) {
	if (!value) {
		return [];
	}
	return value
		.split(/\r?\n/)
		.map((line) => line.trim())
		.filter(Boolean);
};

sync.helpers.ensureModifiedFieldRowsFromLegacy = function (frm) {
	[
		["frappe_modified_field_rows", "frappe_modified_fields"],
		["partner_modified_field_rows", "partner_modified_fields"],
	].forEach(([tableField, legacyField]) => {
		if ((frm.doc[tableField] || []).length) {
			return;
		}
		const fieldnames = sync.helpers.parseDefinitionFieldLines(frm.doc[legacyField]);
		fieldnames.forEach((fieldname) => {
			frm.add_child(tableField, { field_name: fieldname });
		});
		if (fieldnames.length) {
			frm.refresh_field(tableField);
		}
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
	const hasQuery = Boolean((frm.doc.query || "").trim());
	if (hasTableName && hasQuery) {
		return __("Use either Table Name or Query, not both.");
	}
	if (!hasTableName && !hasQuery) {
		return __("Either Table Name or Query is required.");
	}
	if (frm.doc.delete_missing && hasQuery) {
		return __("Delete Missing cannot be enabled when Query is used.");
	}
	return null;
};

sync.helpers.refreshDefinitionSourceValidation = function (frm) {
	const issue = sync.helpers.getDefinitionSourceSettingsIssue(frm);
	if (issue) {
		frm.set_intro(issue, "orange");
		return;
	}
	frm.set_intro("");
};

sync.helpers.validateDefinitionSourceSettings = function (frm) {
	const issue = sync.helpers.getDefinitionSourceSettingsIssue(frm);
	if (issue) {
		frappe.throw(issue);
	}
};
