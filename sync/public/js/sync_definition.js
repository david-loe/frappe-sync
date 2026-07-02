frappe.provide("sync.forms");
sync.helpers = sync.helpers || {};
sync.forms.DEFINITION_PARTNER_COLUMN_METHOD = "get_sync_partner_table_columns";

frappe.ui.form.on("Sync Definition", {
	refresh(frm) {
		sync.helpers.refreshDefinitionFieldPresentation(frm);
		sync.helpers.refreshDefinitionFieldMappingDirection(frm);
		sync.forms.setupButtons(frm);
		sync.helpers.toggleSourceFields(frm);
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
		sync.helpers.normalizeDefinitionDeleteMissing(frm);
		sync.helpers.refreshDefinitionFieldPresentation(frm);
		sync.helpers.refreshDefinitionFieldMappingDirection(frm);
		sync.helpers.refreshDefinitionSourceValidation(frm);
	},
	match_mode(frm) {
		sync.helpers.normalizeDefinitionDeleteMissing(frm);
		sync.helpers.refreshDefinitionFieldPresentation(frm);
		sync.helpers.refreshDefinitionSourceValidation(frm);
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
	before_save(frm) {
		return sync.helpers.confirmDefinitionDeleteMissingBeforeSave(frm);
	},
	validate(frm) {
		sync.helpers.refreshDefinitionFieldPresentation(frm);
		sync.helpers.refreshDefinitionFieldMappingDirection(frm);
		sync.helpers.validateDefinitionSourceSettings(frm);
	},
});

frappe.ui.form.on("Sync Value Mapping", {
	frappe_value_is_null(frm, cdt, cdn) {
		sync.helpers.clearNullValueMappingInput(frm, cdt, cdn, "frappe_value_is_null", "frappe_value");
	},
	partner_value_is_null(frm, cdt, cdn) {
		sync.helpers.clearNullValueMappingInput(frm, cdt, cdn, "partner_value_is_null", "partner_value");
	},
});

frappe.ui.form.on("Sync Field Mapping", {
	mapping_scope(frm, cdt, cdn) {
		sync.helpers.updateDefinitionChildMappingPath(frm, cdt, cdn);
	},
	table_field(frm, cdt, cdn) {
		sync.helpers.updateDefinitionChildMappingPath(frm, cdt, cdn);
		sync.helpers.refreshDefinitionFieldChoices(frm);
	},
	row_idx(frm, cdt, cdn) {
		sync.helpers.updateDefinitionChildMappingPath(frm, cdt, cdn);
	},
	child_field(frm, cdt, cdn) {
		sync.helpers.updateDefinitionChildMappingPath(frm, cdt, cdn);
	},
});

sync.helpers.clearNullValueMappingInput = function (frm, cdt, cdn, nullField, valueField) {
	const row = locals[cdt]?.[cdn];
	if (!row?.[nullField]) {
		return;
	}
	frappe.model.set_value(cdt, cdn, valueField, null);
	frm.refresh_field("value_mapping");
};

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
	frm.add_custom_button(__("Recover Stale Runs"), () => {
		sync.helpers.recoverDefinitionStaleRuns(frm);
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
	if (frm.doc.frappe_modified_field) {
		values.push(frm.doc.frappe_modified_field);
	}
	if (frm.doc.frappe_creation_field) {
		values.push(frm.doc.frappe_creation_field);
	}
	if (frm.doc.frappe_partner_identity_field) {
		values.push(frm.doc.frappe_partner_identity_field);
	}
	return values;
};

sync.helpers.isolateDefinitionGridDocfields = function (grid) {
	if (!grid || !Array.isArray(grid.docfields)) {
		return;
	}

	grid.docfields = grid.docfields.map((df) => ({ ...df }));
	grid.fields_map = {};
	grid.docfields.forEach((df) => {
		if (df?.fieldname) {
			grid.fields_map[df.fieldname] = df;
		}
	});
	grid.__sync_isolated_docfields = true;

	(grid.grid_rows || []).forEach((row) => {
		if (!row) {
			return;
		}
		row.docfields = grid.docfields.map((df) => ({ ...df }));
		row.__sync_isolated_docfields = true;
	});
};

sync.helpers.applyDefinitionGridSelectOverrides = function (grid) {
	if (!grid?.__sync_select_overrides) {
		return;
	}

	sync.helpers.isolateDefinitionGridDocfields(grid);
	Object.entries(grid.__sync_select_overrides).forEach(([fieldname, properties]) => {
		const gridDocfield = grid.docfields.find((df) => df.fieldname === fieldname);
		if (gridDocfield) {
			Object.assign(gridDocfield, properties);
			grid.fields_map[fieldname] = gridDocfield;
		}
		(grid.grid_rows || []).forEach((row) => {
			const rowDocfield = row?.docfields?.find((df) => df.fieldname === fieldname);
			if (rowDocfield) {
				Object.assign(rowDocfield, properties);
			}
			const column = row?.columns?.[fieldname];
			if (column?.df) {
				Object.assign(column.df, properties);
			}
			if (column?.field?.df) {
				Object.assign(column.field.df, properties);
				column.field.refresh();
			}
		});
	});
};

sync.helpers.setupDefinitionGridSelectOverrides = function (grid) {
	if (!grid || grid.__sync_select_override_setup) {
		return;
	}

	const originalRefresh = grid.refresh;
	grid.refresh = function (...args) {
		const result = originalRefresh.apply(this, args);
		sync.helpers.applyDefinitionGridSelectOverrides(this);
		setTimeout(() => sync.helpers.applyDefinitionGridSelectOverrides(this), 0);
		return result;
	};

	const originalAddNewRow = grid.add_new_row;
	grid.add_new_row = function (...args) {
		const result = originalAddNewRow.apply(this, args);
		sync.helpers.applyDefinitionGridSelectOverrides(this);
		setTimeout(() => sync.helpers.applyDefinitionGridSelectOverrides(this), 0);
		return result;
	};

	grid.__sync_select_override_setup = true;
};

sync.helpers.updateDefinitionGridSelect = function (frm, tableField, fieldname, properties) {
	const grid = frm.fields_dict[tableField]?.grid;
	if (!grid) {
		return;
	}

	sync.helpers.setupDefinitionGridSelectOverrides(grid);
	grid.__sync_select_overrides = {
		...(grid.__sync_select_overrides || {}),
		[fieldname]: properties,
	};
	sync.helpers.applyDefinitionGridSelectOverrides(grid);
	frm.refresh_field(tableField);
	sync.helpers.applyDefinitionGridSelectOverrides(grid);
	setTimeout(() => sync.helpers.applyDefinitionGridSelectOverrides(grid), 0);
};

sync.helpers.applyDefinitionFieldChoices = function (frm, fields, tableFields = [], childFields = {}) {
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
	const tableOptions = [
		"",
		...new Set((tableFields || []).map((field) => field?.fieldname).filter(Boolean)),
	].join("\n");
	const childOptions = [
		"",
		...new Set(
			Object.values(childFields || {})
				.flat()
				.map((field) => field?.fieldname)
				.filter(Boolean)
		),
	].join("\n");

	["match_fields", "field_mapping", "value_mapping"].forEach((tableField) => {
		const grid = frm.fields_dict[tableField]?.grid;
		if (!grid) {
			return;
		}
		grid.update_docfield_property("frappe_field", "options", options);
		grid.update_docfield_property("frappe_field", "description", description);
		frm.refresh_field(tableField);
	});

	const fieldMappingGrid = frm.fields_dict.field_mapping?.grid;
	if (fieldMappingGrid) {
		fieldMappingGrid.update_docfield_property("table_field", "options", tableOptions);
		fieldMappingGrid.update_docfield_property("child_field", "options", childOptions);
		frm.refresh_field("field_mapping");
	}

	frm.__sync_table_fields = tableFields || [];
	frm.__sync_child_fields = childFields || {};

	frm.set_df_property("frappe_partner_identity_field", "options", options);
	frm.set_df_property("frappe_partner_identity_field", "description", description);
	frm.refresh_field("frappe_partner_identity_field");

	frm.set_df_property("frappe_modified_field", "options", options);
	frm.set_df_property("frappe_modified_field", "description", description);
	frm.refresh_field("frappe_modified_field");
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

sync.helpers.getDefinitionPartnerColumnSignatureSource = function (frm) {
	return [
		String(frm.doc?.partner || ""),
		String(frm.doc?.table_name || ""),
		String(frm.doc?.read_query || ""),
	];
};

sync.helpers.hashDefinitionPartnerColumnSignatureSource = function (source) {
	const value = JSON.stringify(Array.isArray(source) ? source : []);
	let hashA = 0x811c9dc5;
	let hashB = 0x45d9f3b;
	for (let i = 0; i < value.length; i += 1) {
		const code = value.charCodeAt(i);
		hashA ^= code;
		hashA = Math.imul(hashA, 0x01000193);
		hashB ^= code;
		hashB = Math.imul(hashB, 0x85ebca6b);
	}
	return `v1:${value.length}:${(hashA >>> 0).toString(36)}-${(hashB >>> 0).toString(36)}`;
};

sync.helpers.getDefinitionPartnerColumnSignature = function (frm) {
	return sync.helpers.hashDefinitionPartnerColumnSignatureSource(
		sync.helpers.getDefinitionPartnerColumnSignatureSource(frm)
	);
};

sync.helpers.isDefinitionPartnerColumnReady = function (frm) {
	return Boolean(
		(frm.doc.partner || "").trim() &&
			((frm.doc.table_name || "").trim() || sync.helpers.getDefinitionSourceReadQuery(frm))
	);
};

sync.helpers.getDefinitionPartnerColumnRequestArgs = function (frm) {
	const args = {
		sync_partner_name: frm.doc.partner,
	};
	if ((frm.doc.table_name || "").trim()) {
		args.table_name = frm.doc.table_name;
	}
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
	const partnerTimestampDescription = columns.length
		? __("Select a partner-side timestamp field from the loaded partner source columns.")
		: partnerFieldDescription;

	const fieldMappingGrid = frm.fields_dict.field_mapping?.grid;
	if (fieldMappingGrid) {
		fieldMappingGrid.update_docfield_property("partner_field", "fieldtype", "Select");
		fieldMappingGrid.update_docfield_property("partner_field", "options", partnerFieldOptions);
		fieldMappingGrid.update_docfield_property("partner_field", "ignore_validation", 0);
		fieldMappingGrid.update_docfield_property("partner_field", "description", partnerFieldDescription);
		frm.refresh_field("field_mapping");
	}

	["partner_modified_field", "partner_creation_field"].forEach((fieldname) => {
		frm.set_df_property(fieldname, "options", partnerFieldOptions);
		frm.set_df_property(fieldname, "description", partnerTimestampDescription);
		frm.refresh_field(fieldname);
	});

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
	if (!frm.doc.table_name && !sync.helpers.getDefinitionSourceReadQuery(frm)) {
		missing.push(__("Table Name"));
	}

	$wrapper.html(
		sync.helpers.renderPartnerColumnStatusPanel(state, {
			ready: sync.helpers.isDefinitionPartnerColumnReady(frm),
			missing,
			source_label: sync.helpers.getDefinitionSourceReadQuery(frm) ? __("Read Query") : __("Table Name"),
			source_details: sync.helpers.getDefinitionSourceReadQuery(frm)
				? __("Partner rows are loaded from the Read Query.")
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
			message: __("Set Partner and Table Name or Read Query first."),
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
			const tableFields = Array.isArray(payload.table_fields) ? payload.table_fields : [];
			const childFields = payload.child_fields || {};
			sync.helpers.applyDefinitionFieldChoices(frm, fields, tableFields, childFields);
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

sync.helpers.updateDefinitionChildMappingPath = function (frm, cdt, cdn) {
	const row = locals[cdt]?.[cdn];
	if (!row) {
		return;
	}
	if ((row.mapping_scope || "Parent") !== "Child") {
		return;
	}
	const tableField = String(row.table_field || "").trim();
	const rowIdx = Number.parseInt(row.row_idx || 0, 10);
	const childField = String(row.child_field || "").trim();
	const tableMeta = (frm.__sync_table_fields || []).find((field) => field.fieldname === tableField);
	if (tableMeta?.options && row.child_doctype !== tableMeta.options) {
		frappe.model.set_value(cdt, cdn, "child_doctype", tableMeta.options);
	}
	if (tableField && rowIdx > 0 && childField) {
		frappe.model.set_value(cdt, cdn, "frappe_field", `${tableField}.${rowIdx}.${childField}`);
	}
};

sync.helpers.getDefinitionSourceSettingsIssue = function (frm) {
	const hasTableName = Boolean((frm.doc.table_name || "").trim());
	const hasQuery = Boolean(sync.helpers.getDefinitionSourceReadQuery(frm));
	if (!hasTableName && !sync.helpers.canDefinitionReadQueryReplaceTableName(frm)) {
		return __("Table Name is required.");
	}
	if (frm.doc.delete_missing && hasQuery) {
		return __("Delete Missing cannot be enabled when Read Query is used.");
	}
	return null;
};

sync.helpers.getDefinitionSourceSettingsNotice = function (frm) {
	const notices = [];
	if (frm.doc.delete_missing && !sync.helpers.getDefinitionSourceReadQuery(frm)) {
		notices.push(__("Delete Missing is enabled. Target records absent from a complete source load may be deleted."));
	}
	if (["Queued", "Running"].includes(frm.doc.last_run_status)) {
		notices.push(__("The latest Sync Run is {0}. Stale active runs can block new executions.", [frm.doc.last_run_status]));
	}
	return notices.join(" ");
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

sync.helpers.getDefinitionDeleteMissingSignature = function (frm) {
	return [frm.doc.name || "", frm.doc.sync_type || "", frm.doc.table_name || "", frm.doc.delete_missing ? "1" : "0"].join("::");
};

sync.helpers.confirmDefinitionDeleteMissingBeforeSave = function (frm) {
	if (!frm.doc.delete_missing || sync.helpers.getDefinitionSourceReadQuery(frm)) {
		return Promise.resolve();
	}
	const signature = sync.helpers.getDefinitionDeleteMissingSignature(frm);
	if (frm.__sync_delete_missing_confirmed_signature === signature) {
		return Promise.resolve();
	}
	return new Promise((resolve, reject) => {
		frappe.confirm(
			__(
				"Delete Missing is enabled. A full sync can delete target records that are absent from the source load. Continue saving this Sync Definition?"
			),
			() => {
				frm.__sync_delete_missing_confirmed_signature = signature;
				resolve();
			},
			() => {
				frappe.validated = false;
				reject();
			}
		);
	});
};

sync.helpers.recoverDefinitionStaleRuns = function (frm) {
	if (frm.is_new()) {
		frappe.msgprint(__("Save this Sync Definition before recovering stale runs."));
		return;
	}
	sync.helpers
		.callApi(
			"recover_stale_runs",
			{ sync_definition_name: frm.doc.name },
			{ freeze_message: __("Recovering stale runs…") }
		)
		.then((response) => {
			const payload = response?.message || {};
			frappe.show_alert({
				message: __("Recovered {0} stale runs.", [cint(payload.recovered_count)]),
				indicator: payload.recovered_count ? "green" : "blue",
			});
			frm.reload_doc();
		})
		.catch((error) => {
			frappe.msgprint(sync.helpers.extractApiErrorMessage(error) || __("Unable to recover stale runs."));
		});
};
