frappe.provide("sync");
frappe.provide("sync.helpers");

sync.helpers.callApi = function (method, args = {}, opts = {}) {
	// eslint-disable-next-line new-cap
	return frappe.call({
		method: `sync.api.${method}`,
		args,
		freeze: opts.freeze ?? true,
		freeze_message: opts.freeze_message,
		callback: opts.callback,
		error: opts.error,
	});
};

sync.helpers.getList = function (doctype, args = {}) {
	const listArgs = Object.assign({}, args);
	if (listArgs.limit == null && listArgs.limit_page_length != null) {
		listArgs.limit = listArgs.limit_page_length;
		delete listArgs.limit_page_length;
	}
	return frappe.db.get_list(doctype, listArgs);
};

sync.helpers.getSyncDefinitionDoctype = function (syncDefinitionName, opts = {}) {
	const name = String(syncDefinitionName || "").trim();
	if (!name) {
		return Promise.resolve("");
	}

	const useCache = opts.use_cache !== false;
	sync.helpers._sync_definition_doctype_cache = sync.helpers._sync_definition_doctype_cache || {};
	sync.helpers._sync_definition_doctype_promises = sync.helpers._sync_definition_doctype_promises || {};

	if (useCache && Object.prototype.hasOwnProperty.call(sync.helpers._sync_definition_doctype_cache, name)) {
		return Promise.resolve(sync.helpers._sync_definition_doctype_cache[name]);
	}

	if (useCache && sync.helpers._sync_definition_doctype_promises[name]) {
		return sync.helpers._sync_definition_doctype_promises[name];
	}

	const request = frappe.db
		.get_value("Sync Definition", name, "doctype_name")
		.then((response) => {
			const payload = response?.message;
			const doctypeName = typeof payload === "string" ? payload : payload?.doctype_name || "";
			sync.helpers._sync_definition_doctype_cache[name] = doctypeName;
			return doctypeName;
		})
		.catch(() => "")
		.finally(() => {
			delete sync.helpers._sync_definition_doctype_promises[name];
		});

	if (useCache) {
		sync.helpers._sync_definition_doctype_promises[name] = request;
	}

	return request;
};

sync.helpers.persistDocValues = function (doctype, name, values) {
	return frappe.call({
		method: "frappe.client.set_value",
		args: {
			doctype,
			name,
			fieldname: values,
		},
		freeze: false,
	});
};

sync.helpers.extractApiErrorMessage = function (error) {
	const directMessage =
		error?.message ||
		error?.exc ||
		error?.exception ||
		error?.responseJSON?.message ||
		error?.responseJSON?.exc;
	if (directMessage) {
		return directMessage;
	}

	const serverMessages = error?._server_messages || error?.responseJSON?._server_messages;
	if (!serverMessages) {
		return __("Request failed.");
	}

	try {
		const parsed = JSON.parse(serverMessages);
		for (const entry of parsed || []) {
			if (!entry) {
				continue;
			}
			if (typeof entry === "string") {
				try {
					const nested = JSON.parse(entry);
					if (nested?.message) {
						return nested.message;
					}
				} catch (nestedError) {
					return entry;
				}
				return entry;
			}
			if (entry.message) {
				return entry.message;
			}
		}
	} catch (parseError) {
		return serverMessages;
	}

	return __("Request failed.");
};

sync.helpers.isMissingApiMethodError = function (error) {
	const message = sync.helpers.extractApiErrorMessage(error).toLowerCase();
	return (
		message.includes("failed to get method") ||
		message.includes("failed to get method for command") ||
		message.includes("does not exist in module") ||
		message.includes("does not exist") ||
		message.includes("is not whitelisted")
	);
};

sync.helpers.getDefinitionSourceReadQuery = function (frm) {
	return String(frm.doc?.read_query || "").trim();
};

sync.helpers.setDefinitionFieldProperty = function (frm, fieldname, property, value) {
	if (!fieldname || !frm?.fields_dict?.[fieldname]) {
		return;
	}
	frm.set_df_property(fieldname, property, value);
};

sync.helpers.toggleDefinitionField = function (frm, fieldname, visible, reqd) {
	if (!fieldname || !frm?.fields_dict?.[fieldname]) {
		return;
	}
	frm.toggle_display(fieldname, visible);
	if (reqd !== undefined) {
		frm.toggle_reqd(fieldname, reqd);
	}
};

sync.helpers.refreshDefinitionFieldPresentation = function (frm) {
	const hasReadQuery = Boolean(sync.helpers.getDefinitionSourceReadQuery(frm));
	const canWritePartner = (frm.doc.sync_type || "").toLowerCase() !== "a<-b";
	const identityFieldNames = [
		"partner_identity_field",
		"frappe_partner_identity_field",
		"partner_frappe_identity_field",
		"partner_create_id_strategy",
		"partner_create_id_source",
		"partner_create_id_scope_where",
	];

	sync.helpers.setDefinitionFieldProperty(
		frm,
		"table_name",
		"label",
		__("Table Name")
	);
	sync.helpers.setDefinitionFieldProperty(
		frm,
		"table_name",
		"description",
		hasReadQuery
			? __("Writable partner table. Required for inserts, updates, deletes, and column introspection. Read Query is active, so reads are limited by the query while writes still target Table Name.")
			: __("Writable partner table. Required for inserts, updates, deletes, and column introspection. Leave Read Query blank to read the full table.")
	);

	sync.helpers.setDefinitionFieldProperty(
		frm,
		"read_query",
		"label",
		__("Read Query")
	);
	sync.helpers.setDefinitionFieldProperty(
		frm,
		"read_query",
		"description",
		__("Optional read-only query used to load partner rows. Leave blank to read the full table. Writes still target Table Name.")
	);
	sync.helpers.setDefinitionFieldProperty(
		frm,
		"match_fields",
		"label",
		__("Match Fields")
	);
	sync.helpers.setDefinitionFieldProperty(
		frm,
		"match_fields",
		"description",
		__("Logical fields used to match source and partner records. These are not the partner's technical identity field.")
	);

	identityFieldNames.forEach((fieldname) => {
		if (!frm?.fields_dict?.[fieldname]) {
			return;
		}
		switch (fieldname) {
			case "partner_identity_field":
				sync.helpers.setDefinitionFieldProperty(
					frm,
					fieldname,
					"label",
					__("Partner Identity Field")
				);
				sync.helpers.setDefinitionFieldProperty(
					frm,
					fieldname,
					"description",
					__("Technical partner key column used when a new partner row is created.")
				);
				sync.helpers.toggleDefinitionField(frm, fieldname, canWritePartner, false);
				break;
			case "frappe_partner_identity_field":
				sync.helpers.setDefinitionFieldProperty(
					frm,
					fieldname,
					"label",
					__("Frappe Partner ID Field")
				);
				sync.helpers.setDefinitionFieldProperty(
					frm,
					fieldname,
					"description",
					__("Optional Frappe field that stores the partner-generated identity after create.")
				);
				sync.helpers.toggleDefinitionField(frm, fieldname, canWritePartner, false);
				break;
			case "partner_frappe_identity_field":
				sync.helpers.setDefinitionFieldProperty(
					frm,
					fieldname,
					"label",
					__("Partner Frappe ID Field")
				);
				sync.helpers.setDefinitionFieldProperty(
					frm,
					fieldname,
					"description",
					__("Optional partner field that stores the Frappe document name for traceability.")
				);
				sync.helpers.toggleDefinitionField(frm, fieldname, canWritePartner, false);
				break;
			case "partner_create_id_strategy":
				sync.helpers.setDefinitionFieldProperty(
					frm,
					fieldname,
					"label",
					__("Partner Create ID Strategy")
				);
				sync.helpers.setDefinitionFieldProperty(
					frm,
					fieldname,
					"description",
					__("Controls how a new partner identity is produced when the partner does not generate it from the payload.")
				);
				sync.helpers.toggleDefinitionField(frm, fieldname, canWritePartner, false);
				break;
			case "partner_create_id_source":
				sync.helpers.setDefinitionFieldProperty(
					frm,
					fieldname,
					"label",
					__("Partner Create ID Source")
				);
				sync.helpers.setDefinitionFieldProperty(
					frm,
					fieldname,
					"description",
					__("Additional strategy input such as a database sequence name or allocation source.")
				);
				sync.helpers.toggleDefinitionField(frm, fieldname, canWritePartner, false);
				break;
			case "partner_create_id_scope_where":
				sync.helpers.setDefinitionFieldProperty(
					frm,
					fieldname,
					"label",
					__("Partner Create ID Scope")
				);
				sync.helpers.setDefinitionFieldProperty(
					frm,
					fieldname,
					"description",
					__("SQL WHERE condition that limits the rows considered when generating the next partner ID, for example `NR BETWEEN 1 AND 89999`.")
				);
				sync.helpers.toggleDefinitionField(frm, fieldname, canWritePartner, false);
				break;
			default:
				break;
		}
	});
};

sync.helpers.normalizePartnerColumnChoices = function (payload) {
	const candidates = Array.isArray(payload)
		? payload
		: [
				payload?.columns,
				payload?.partner_columns,
				payload?.table_columns,
				payload?.fields,
				payload?.data,
		  ].find(Array.isArray) || [];

	return candidates
		.map((entry) => {
			if (!entry) {
				return null;
			}
			if (typeof entry === "string") {
				return { label: entry, value: entry };
			}

			const value = String(
				entry.value ||
				entry.name ||
				entry.fieldname ||
				entry.column_name ||
				entry.column ||
				entry.label ||
				""
			).trim();
			if (!value) {
				return null;
			}

			const label = String(entry.label || value).trim() || value;
			const descriptionBits = [
				entry.fieldtype || entry.type || entry.data_type || entry.db_type,
				entry.nullable === false || entry.required ? __("Not Null") : "",
				entry.length ? __("Length {0}", [entry.length]) : "",
			].filter(Boolean);

			return {
				label,
				value,
				description: descriptionBits.join(" · "),
			};
		})
		.filter(Boolean);
};

sync.helpers.renderPreviewList = function (items, emptyMessage = __("None")) {
	const values = Array.isArray(items) ? items.filter(Boolean) : [];
	if (!values.length) {
		return `<div class="text-muted">${frappe.utils.escape_html(emptyMessage)}</div>`;
	}

	const rows = values
		.map((item) => `<li>${sync.helpers.renderPreviewValue(item)}</li>`)
		.join("");
	return `<ul class="mb-0 ps-3">${rows}</ul>`;
};

sync.helpers.renderPartnerColumnStatusPanel = function (state, context = {}) {
	const ready = Boolean(context.ready);
	const columns = Array.isArray(state?.columns) ? state.columns : [];
	const missing = Array.isArray(context.missing) ? context.missing.filter(Boolean) : [];
	const sourceLabel = context.source_label || __("current source");
	const sourceDetails = context.source_details || "";
	const previewColumns = columns.slice(0, 12);
	const buttonLabel = columns.length ? __("Refresh Columns") : __("Load Columns");
	const button = ready
		? `<button type="button" class="btn btn-sm btn-secondary" data-action="refresh-partner-columns">${frappe.utils.escape_html(buttonLabel)}</button>`
		: "";

	if (!ready) {
		const details = missing.length
			? __("Set {0} first.", [missing.join(", ")])
			: __("Partner column loading is only available after a partner and table name are set.");
		return `
			<div class="border rounded p-3 bg-light">
				<div class="d-flex justify-content-between align-items-start gap-3">
					<div>
						<div class="fw-semibold mb-1">${__("Partner Columns")}</div>
						<div class="text-muted small">${frappe.utils.escape_html(details)}</div>
					</div>
				</div>
			</div>
		`;
	}

	if (state?.loading) {
		return `
			<div class="border rounded p-3 bg-light">
				<div class="d-flex justify-content-between align-items-start gap-3">
					<div>
						<div class="fw-semibold mb-1">${__("Partner Columns")}</div>
						<div class="text-muted small">${__("Loading partner columns from the configured source...")}</div>
					</div>
					<div class="text-muted small">${__("Working")}</div>
				</div>
			</div>
		`;
	}

	const statusClass = state?.error ? "border-danger-subtle" : state?.stale ? "border-warning-subtle" : "border-success-subtle";
	const statusText = state?.error
		? state.error
		: state?.stale
			? __("Source settings changed. Refresh the column list before editing partner-side mappings.")
			: columns.length
				? __("Loaded {0} partner columns from {1}.", [columns.length, sourceLabel])
				: __("Load partner columns from {0} to get guided partner-side field selection.", [sourceLabel]);
	const refreshedAt = state?.loaded_at ? __("Last refresh: {0}", [state.loaded_at]) : "";
	const columnBadges = previewColumns
		.map((column) => {
			const title = column.description ? ` title="${frappe.utils.escape_html(column.description)}"` : "";
			return `<span class="badge bg-secondary-subtle text-secondary border"${title}>${frappe.utils.escape_html(column.label || column.value)}</span>`;
		})
		.join(" ");
	const overflow = columns.length > previewColumns.length ? `<span class="text-muted small">+${columns.length - previewColumns.length} ${__("more")}</span>` : "";

	return `
		<div class="border rounded p-3 ${statusClass}">
			<div class="d-flex justify-content-between align-items-start gap-3">
				<div class="flex-grow-1">
					<div class="fw-semibold mb-1">${__("Partner Columns")}</div>
					<div class="small ${state?.error ? "text-danger" : state?.stale ? "text-warning-emphasis" : "text-muted"}">${frappe.utils.escape_html(statusText)}</div>
					${sourceDetails ? `<div class="small text-muted mt-1">${frappe.utils.escape_html(sourceDetails)}</div>` : ""}
					${refreshedAt ? `<div class="small text-muted mt-1">${frappe.utils.escape_html(refreshedAt)}</div>` : ""}
				</div>
				${button}
			</div>
			${columnBadges ? `<div class="mt-3 d-flex flex-wrap gap-2">${columnBadges} ${overflow}</div>` : ""}
		</div>
	`;
};

sync.helpers.getRunSyncDefinitionMessage = function (payload, options = {}) {
	const dryRun = Boolean(options.dry_run);
	if (typeof payload === "string" && payload.trim()) {
		return payload;
	}

	const status = String(payload?.status || "").trim();
	const runName = String(payload?.run || "").trim();
	const definitionName = String(payload?.sync_definition || options.sync_definition_name || "").trim();
	const label = dryRun ? __("Dry run") : __("Run");

	if (status === "already_running") {
		return __("A sync run is already active for {0}.", [definitionName || __("this definition")]);
	}
	if (status === "success") {
		return runName ? __("{0} completed: {1}", [label, runName]) : __("{0} completed.", [label]);
	}
	if (status === "queued") {
		return runName ? __("{0} queued: {1}", [label, runName]) : __("{0} queued.", [label]);
	}
	return dryRun ? __("Dry run started.") : __("Sync started.");
};

sync.helpers.getRunSyncDefinitionIndicator = function (payload) {
	const status = String(payload?.status || "").trim();
	if (status === "already_running") {
		return "orange";
	}
	if (status === "error") {
		return "red";
	}
	return "green";
};

sync.helpers.runSyncDefinition = function (frm, trigger = "manual", options = {}) {
	const dryRun = Boolean(options.dry_run);
	const ensureSaved = frm.is_new() || frm.is_dirty() ? frm.save() : Promise.resolve();

	return ensureSaved
		.then(() =>
			sync.helpers.callApi(
				"run_sync_definition",
				{ sync_definition_name: frm.doc.name, trigger, dry_run: dryRun },
				{ freeze_message: dryRun ? __("Queueing dry run…") : __("Queueing sync run…") }
			)
		)
		.then((response) => {
			const payload = response?.message;
			frappe.show_alert({
				message: sync.helpers.getRunSyncDefinitionMessage(payload, {
					dry_run: dryRun,
					sync_definition_name: frm.doc.name,
				}),
				indicator: sync.helpers.getRunSyncDefinitionIndicator(payload),
			});
		})
		.catch((error) => frappe.msgprint(sync.helpers.extractApiErrorMessage(error) || __("Unable to run sync definition")));
};

sync.helpers.previewSyncDefinition = function (frm) {
	const limit = frm.doc.preview_limit || 50;
	sync.helpers.callApi("preview_sync_definition", { sync_definition_name: frm.doc.name, limit }, { freeze_message: "Preparing preview…" })
		.then((response) => {
			const payload = response?.message;
			frappe.msgprint({
				title: __("Sync Preview"),
				message: sync.helpers.renderPreviewModal(payload),
				indicator: "blue",
			});
		})
		.catch((error) => frappe.msgprint(error?.message ?? "Unable to generate preview"));
};

sync.helpers.renderPreviewModal = function (payload) {
	if (typeof payload === "string") {
		return `<div class="text-muted">${frappe.utils.escape_html(payload)}</div>`;
	}

	const data = payload || {};
	const ping = data.partner_ping || {};
	const records = Array.isArray(data.frappe_records_sample) ? data.frappe_records_sample : [];
	const mapping = sync.helpers.getPreviewMapping(data);
	const summaryRows = [
		["Sync Definition", data.sync_definition],
		["Sync Type", data.sync_type],
		["Partner", data.partner],
		["Connector", data.connector],
		["Sample Count", data.frappe_records_sample_count],
		["Partner Ping", ping.ok === false ? __("Error") : __("OK")],
		["Ping Message", ping.message],
	];

	const sections = [
		`
			<div class="mb-3">
				<h5 class="mb-2">${__("Summary")}</h5>
				${sync.helpers.renderKeyValueTable(summaryRows)}
			</div>
		`,
		`
			<div class="mb-3">
				<h5 class="mb-2">${__("Fields")}</h5>
				<div class="d-flex flex-wrap gap-2">
					${sync.helpers.renderChipList(__("Match Fields"), data.match_fields)}
					${sync.helpers.renderChipList(__("Value Mapping"), data.value_mapping_fields)}
				</div>
			</div>
		`,
		`
			<div class="mb-3">
				<h5 class="mb-2">${__("Mapping")}</h5>
				${sync.helpers.renderMappingTable(mapping)}
			</div>
		`,
		`
			<div class="mb-3">
				<h5 class="mb-2">${__("Actions")}</h5>
				${sync.helpers.renderActionsTable(data.actions)}
			</div>
		`,
		`
			<div class="mb-3">
				<h5 class="mb-2">${__("Sample Records")}</h5>
				${sync.helpers.renderSampleRecordsTable(records)}
			</div>
		`,
		`
			<details class="mb-1">
				<summary class="text-muted">${__("Raw JSON")}</summary>
				<pre class="mt-2">${frappe.utils.escape_html(JSON.stringify(data, null, 2))}</pre>
			</details>
		`,
	];

	return `<div class="sync-preview">${sections.join("")}</div>`;
};

sync.helpers.renderKeyValueTable = function (rows) {
	const cells = (rows || [])
		.filter((row) => row && row.some((value) => value !== undefined && value !== null && value !== ""))
		.map(
			([label, value]) => `
				<tr>
					<th class="text-muted" style="width: 30%;">${frappe.utils.escape_html(label)}</th>
					<td>${sync.helpers.renderPreviewValue(value)}</td>
				</tr>
			`
		)
		.join("");

	if (!cells) {
		return `<div class="text-muted">${__("No summary available.")}</div>`;
	}

	return `<div class="table-responsive"><table class="table table-bordered table-sm mb-0"><tbody>${cells}</tbody></table></div>`;
};

sync.helpers.renderChipList = function (label, values) {
	const items = (Array.isArray(values) ? values : [])
		.filter((value) => value !== undefined && value !== null && value !== "")
		.map((value) => `<span class="badge bg-secondary-subtle text-secondary border">${frappe.utils.escape_html(String(value))}</span>`)
		.join(" ");

	return `
		<div class="flex-grow-1">
			<div class="small text-muted mb-1">${frappe.utils.escape_html(label)}</div>
			<div>${items || `<span class="text-muted">${__("None")}</span>`}</div>
		</div>
	`;
};

sync.helpers.getPreviewMapping = function (payload) {
	const data = payload || {};
	const nestedSyncDefinition =
		data.sync_definition && typeof data.sync_definition === "object" ? data.sync_definition : null;
	const source =
		data.mapping ??
		data.field_mapping ??
		nestedSyncDefinition?.mapping ??
		nestedSyncDefinition?.field_mapping ??
		{};
	const structuredSource =
		Array.isArray(source) ||
		(
			source &&
			typeof source === "object" &&
			Object.values(source).some((entry) => entry && typeof entry === "object")
		);
	return sync.helpers.normalizeFieldMapping(source, {
		default_direction: structuredSource ? "Both" : "",
	});
};

sync.helpers.normalizeFieldMappingDirection = function (direction, options = {}) {
	const fallback = options.default_direction ?? "";
	const value = String(direction || "").trim();
	const allowed = new Set(["Both", "Frappe to Partner", "Partner to Frappe"]);
	if (!value) {
		return fallback;
	}
	return allowed.has(value) ? value : fallback;
};

sync.helpers.normalizeFieldMapping = function (mapping, options = {}) {
	const normalized = {};
	const addEntry = (frappeField, partnerField, direction) => {
		const sourceField = String(frappeField || "").trim();
		const targetField = String(partnerField || "").trim();
		if (!sourceField || !targetField) {
			return;
		}
		normalized[sourceField] = {
			partner_field: targetField,
			direction: sync.helpers.normalizeFieldMappingDirection(direction, options),
		};
	};

	if (Array.isArray(mapping)) {
		mapping.forEach((entry) => {
			if (!entry || typeof entry !== "object") {
				return;
			}
			addEntry(
				entry.frappe_field ?? entry.frappeField ?? entry.source_field ?? entry.field_name,
				entry.partner_field ?? entry.partnerField ?? entry.target_field ?? entry.column_name,
				entry.direction
			);
		});
		return normalized;
	}

	if (!mapping || typeof mapping !== "object") {
		return normalized;
	}

	Object.entries(mapping).forEach(([frappeField, entry]) => {
		if (entry && typeof entry === "object" && !Array.isArray(entry)) {
			addEntry(
				frappeField,
				entry.partner_field ?? entry.partnerField ?? entry.partner ?? entry.target_field ?? entry.value,
				entry.direction
			);
			return;
		}
		addEntry(frappeField, entry, options.default_direction);
	});

	return normalized;
};

sync.helpers.renderMappingTable = function (mapping) {
	const entries = Object.entries(sync.helpers.normalizeFieldMapping(mapping));
	if (!entries.length) {
		return `<div class="text-muted">${__("No field mapping configured.")}</div>`;
	}

	const rows = entries
		.map(([frappeField, target]) => {
			const partnerField = target?.partner_field ?? target?.partnerField ?? "";
			const direction = target?.direction ?? "";
			return `
				<tr>
					<td>${frappe.utils.escape_html(frappeField)}</td>
					<td>${sync.helpers.renderPreviewValue(partnerField)}</td>
					<td>${sync.helpers.renderPreviewValue(direction)}</td>
				</tr>
			`;
		})
		.join("");

	return `
		<div class="table-responsive">
			<table class="table table-bordered table-sm mb-0">
				<thead>
					<tr>
						<th>${__("Frappe Field")}</th>
						<th>${__("Partner Field")}</th>
						<th>${__("Direction")}</th>
					</tr>
				</thead>
				<tbody>${rows}</tbody>
			</table>
		</div>
	`;
};

sync.helpers.renderActionsTable = function (actions) {
	const rows = (Array.isArray(actions) ? actions : [])
		.map((action) => {
			if (!action || typeof action !== "object") {
				return "";
			}
			return `
				<tr>
					<td>${sync.helpers.renderPreviewValue(action.direction)}</td>
					<td>${sync.helpers.renderPreviewValue(action.result)}</td>
				</tr>
			`;
		})
		.join("");

	if (!rows) {
		return `<div class="text-muted">${__("No actions available.")}</div>`;
	}

	return `
		<div class="table-responsive">
			<table class="table table-bordered table-sm mb-0">
				<thead>
					<tr>
						<th>${__("Direction")}</th>
						<th>${__("Result")}</th>
					</tr>
				</thead>
				<tbody>${rows}</tbody>
			</table>
		</div>
	`;
};

sync.helpers.renderSampleRecordsTable = function (records) {
	if (!records.length) {
		return `<div class="text-muted">${__("No sample records returned.")}</div>`;
	}

	const columns = sync.helpers.getPreviewColumns(records);
	const header = columns.map((column) => `<th>${frappe.utils.escape_html(column)}</th>`).join("");
	const body = records
		.map((record) => {
			const cells = columns
				.map((column) => `<td>${sync.helpers.renderPreviewValue(record?.[column])}</td>`)
				.join("");
			return `<tr>${cells}</tr>`;
		})
		.join("");

	return `
		<div class="table-responsive">
			<table class="table table-bordered table-sm mb-0">
				<thead><tr>${header}</tr></thead>
				<tbody>${body}</tbody>
			</table>
		</div>
	`;
};

sync.helpers.getPreviewColumns = function (records) {
	const preferred = ["name", "modified", "docstatus"];
	const seen = new Set();
	const columns = [];

	for (const column of preferred) {
		if (records.some((record) => Object.prototype.hasOwnProperty.call(record || {}, column))) {
			columns.push(column);
			seen.add(column);
		}
	}

	for (const record of records) {
		for (const column of Object.keys(record || {})) {
			if (seen.has(column)) {
				continue;
			}
			seen.add(column);
			columns.push(column);
			if (columns.length >= 8) {
				return columns;
			}
		}
	}

	return columns;
};

sync.helpers.renderPreviewValue = function (value) {
	if (value === undefined || value === null || value === "") {
		return `<span class="text-muted">${__("None")}</span>`;
	}
	if (typeof value === "object") {
		return `<pre class="mb-0">${frappe.utils.escape_html(JSON.stringify(value, null, 2))}</pre>`;
	}
	return frappe.utils.escape_html(String(value));
};

sync.helpers.exportDefinitionYaml = function (frm) {
	sync.helpers.callApi("export_sync_definition_yaml", { sync_definition_name: frm.doc.name }, { freeze_message: "Generating YAML…" })
		.then((response) => {
			const yaml = response?.message || "";
			if (!yaml) {
				frappe.msgprint(__("No YAML returned."));
				return;
			}
			const dialog = frappe.prompt(
				{
					fieldtype: "Code",
					fieldname: "yaml",
					label: __("YAML Export"),
					description: __("Copy the YAML below to archive or share."),
					read_only: 1,
					wrap: true,
					min_lines: 16,
					max_lines: 30,
					default: yaml,
				},
				() => {},
				__("YAML Export"),
				__("Close")
			);
			dialog.set_value("yaml", yaml);
			dialog.fields_dict.yaml?.set_focus?.();
	})
		.catch((error) => frappe.msgprint(error?.message ?? "Unable to export YAML"));
};

sync.helpers.getImportPreviewSummaryRows = function (payload, values, options = {}) {
	const data = payload || {};
	const conflicts = sync.helpers.getImportPreviewConflicts(data);
	const warnings = sync.helpers.getImportPreviewWarnings(data);
	const summary = sync.helpers.getImportPreviewSummary(data);
	const yamlText = String(values?.yaml || "");
	const yamlLines = yamlText ? yamlText.split(/\r?\n/).length : 0;

	return [
		["Preview Endpoint", options.preview_available === false ? __("Fallback review only") : __("Available")],
		["Definition", data.sync_definition || data.sync_definition_name || data.title || data.name],
		["Existing Definition", data.existing_sync_definition || data.existing_name || data.existing_definition],
		["Overwrite Existing", values?.overwrite ? __("Yes") : __("No")],
		["Can Import", data.can_import ?? data.allowed ?? data.ok],
		["Conflict Count", Array.isArray(conflicts) ? conflicts.length : 0],
		["Warning Count", Array.isArray(warnings) ? warnings.length : 0],
		["Create", summary.create || 0],
		["Update", summary.update || 0],
		["Invalid", summary.invalid || 0],
		["Missing Payload", summary.missing_payload || 0],
		["YAML Lines", yamlLines || ""],
	];
};

sync.helpers.renderImportPreviewModal = function (payload, values, options = {}) {
	const data = payload || {};
	const conflicts = sync.helpers.getImportPreviewConflicts(data);
	const warnings = sync.helpers.getImportPreviewWarnings(data);
	const actions = sync.helpers.getImportPreviewActions(data);
	const documents = sync.helpers.getImportPreviewDocuments(data);
	const sections = [
		`
			<div class="mb-3">
				<h5 class="mb-2">${__("Summary")}</h5>
				${sync.helpers.renderKeyValueTable(sync.helpers.getImportPreviewSummaryRows(data, values, options))}
			</div>
		`,
		options.preview_available === false
			? `
				<div class="alert alert-warning mb-3">
					${__("Server-side import preview is not available yet. You can still import, but conflict hints are limited to this client-side review step.")}
				</div>
			`
			: "",
		`
			<div class="mb-3">
				<h5 class="mb-2">${__("Conflict Hints")}</h5>
				${sync.helpers.renderPreviewList(conflicts, __("No conflicts reported."))}
			</div>
		`,
		`
			<div class="mb-3">
				<h5 class="mb-2">${__("Warnings")}</h5>
				${sync.helpers.renderPreviewList(warnings, __("No warnings reported."))}
			</div>
		`,
		`
			<div class="mb-3">
				<h5 class="mb-2">${__("Planned Changes")}</h5>
				${sync.helpers.renderImportDocumentTable(documents, actions)}
			</div>
		`,
		`
			<details class="mb-1">
				<summary class="text-muted">${__("Raw Preview Data")}</summary>
				<pre class="mt-2">${frappe.utils.escape_html(JSON.stringify(data, null, 2))}</pre>
			</details>
		`,
	].filter(Boolean);

	return `<div class="sync-import-preview">${sections.join("")}</div>`;
};

sync.helpers.getImportPreviewSummary = function (payload) {
	const summary = payload?.summary;
	return summary && typeof summary === "object" ? summary : {};
};

sync.helpers.getImportPreviewDocuments = function (payload) {
	const source = payload?.documents || payload?.document_plan || payload?.document_summary;
	if (!source || typeof source !== "object") {
		return [];
	}

	if (Array.isArray(source)) {
		return source.filter((entry) => entry && typeof entry === "object");
	}

	return Object.entries(source)
		.map(([doctype, entry]) => {
			if (!entry || typeof entry !== "object") {
				return null;
			}
			return Object.assign({ doctype }, entry);
		})
		.filter(Boolean);
};

sync.helpers.getImportPreviewConflicts = function (payload) {
	const direct = payload?.conflicts || payload?.conflict_hints || payload?.conflict_messages || payload?.issues;
	if (Array.isArray(direct) && direct.length) {
		return direct;
	}

	return sync.helpers
		.getImportPreviewDocuments(payload)
		.filter((entry) => entry.status === "conflict")
		.map((entry) => entry.hint || __("Conflict for {0}", [entry.doctype || entry.name || __("document")]));
};

sync.helpers.getImportPreviewWarnings = function (payload) {
	const warnings = [];
	const directWarnings = payload?.warnings || payload?.hints || payload?.messages;
	if (Array.isArray(directWarnings)) {
		warnings.push(...directWarnings);
	}
	if (payload?.error) {
		warnings.push(payload.error);
	}
	for (const part of payload?.missing_payload_parts || []) {
		warnings.push(__("Missing payload section: {0}", [part]));
	}
	for (const entry of sync.helpers.getImportPreviewDocuments(payload)) {
		if (entry.status === "invalid" || entry.status === "missing_payload") {
			warnings.push(entry.hint || __("Import issue for {0}", [entry.doctype || __("document")]));
		}
	}
	return warnings.filter(Boolean);
};

sync.helpers.getImportPreviewActions = function (payload) {
	const direct = payload?.actions || payload?.planned_actions || payload?.document_actions;
	if (Array.isArray(direct) && direct.length) {
		return direct;
	}

	return sync.helpers.getImportPreviewDocuments(payload).map((entry) => ({
		doctype: entry.doctype,
		name: entry.name,
		action: entry.action,
		status: entry.status,
		hint: entry.hint,
	}));
};

sync.helpers.renderImportDocumentTable = function (documents, actions) {
	const rows = Array.isArray(documents) ? documents.filter(Boolean) : [];
	if (!rows.length) {
		return sync.helpers.renderPreviewValue(actions.length ? actions : __("No planned changes returned."));
	}

	const body = rows
		.map((entry) => {
			const label = [entry.doctype, entry.name].filter(Boolean).join(" · ");
			return `
				<tr>
					<td>${frappe.utils.escape_html(label || __("Document"))}</td>
					<td>${sync.helpers.renderPreviewValue(entry.status || __("Unknown"))}</td>
					<td>${sync.helpers.renderPreviewValue(entry.action || __("None"))}</td>
					<td>${sync.helpers.renderPreviewValue(entry.hint || "")}</td>
				</tr>
			`;
		})
		.join("");

	return `
		<div class="table-responsive">
			<table class="table table-bordered table-sm mb-0">
				<thead>
					<tr>
						<th>${__("Document")}</th>
						<th>${__("Status")}</th>
						<th>${__("Action")}</th>
						<th>${__("Hint")}</th>
					</tr>
				</thead>
				<tbody>${body}</tbody>
			</table>
		</div>
	`;
};

sync.helpers.executeDefinitionYamlImport = function (frm, values) {
	return sync.helpers
		.callApi(
			"import_sync_definition_yaml",
			{
				yaml_payload: values.yaml,
				overwrite: values.overwrite,
			},
			{ freeze_message: "Importing YAML…" }
		)
		.then((response) => {
			const message = response?.message;
			frappe.msgprint({
				title: __("Import complete"),
				message:
					typeof message === "string" && message
						? __("Sync definition imported as {0}.", [message])
						: message || __("Sync definition imported."),
				indicator: "green",
			});
			if (typeof message === "string" && message && message !== frm.doc.name) {
				frappe.set_route("Form", "Sync Definition", message);
				return;
			}
			frm.reload_doc();
		})
		.catch((error) => {
			frappe.msgprint(sync.helpers.extractApiErrorMessage(error));
			return Promise.reject(error);
		});
};

sync.helpers.showDefinitionYamlImportPreview = function (frm, values, payload, options = {}) {
	const canImport = options.preview_available === false || payload?.can_import !== false;
	const dialog = new frappe.ui.Dialog({
		title: __("Import Preview"),
		fields: [{ fieldtype: "HTML", fieldname: "preview" }],
		primary_action_label: canImport ? __("Import") : __("Import Blocked"),
		primary_action() {
			if (!canImport) {
				return;
			}
			dialog.disable_primary_action();
			sync.helpers
				.executeDefinitionYamlImport(frm, values)
				.then(() => dialog.hide())
				.catch(() => dialog.enable_primary_action());
		},
		secondary_action_label: __("Adjust YAML"),
		secondary_action() {
			dialog.hide();
			sync.helpers.importDefinitionYaml(frm, values);
		},
	});
	dialog.fields_dict.preview.$wrapper.html(sync.helpers.renderImportPreviewModal(payload, values, options));
	dialog.show();
	if (!canImport) {
		dialog.disable_primary_action();
	}
};

sync.helpers.previewDefinitionYamlImport = function (frm, values) {
	const args = {
		yaml_payload: values.yaml,
		yaml: values.yaml,
		overwrite: values.overwrite,
	};

	return sync.helpers
		.callApi("preview_import_sync_definition_yaml", args, { freeze_message: "Preparing import preview…" })
		.then((response) => {
			sync.helpers.showDefinitionYamlImportPreview(frm, values, response?.message || {}, {
				preview_available: true,
			});
		})
		.catch((error) => {
			frappe.msgprint(sync.helpers.extractApiErrorMessage(error));
		});
};

sync.helpers.importDefinitionYaml = function (frm, initialValues = {}) {
	frappe.prompt(
		[
			{
				fieldtype: "Code",
				fieldname: "yaml",
				label: __("YAML Definition"),
				description: __("Paste a YAML export of a sync definition to import."),
				reqd: 1,
				wrap: true,
				min_lines: 16,
				max_lines: 30,
				default: initialValues.yaml || "",
			},
			{
				fieldtype: "Check",
				fieldname: "overwrite",
				label: __("Overwrite existing"),
				description: __("Replace the existing definition if the name already exists."),
				default: initialValues.overwrite ? 1 : 0,
			},
		],
		(values) => {
			sync.helpers.previewDefinitionYamlImport(frm, values);
		},
		__("Import Sync Definition"),
		__("Preview")
	);
};

sync.helpers.testPartnerConnection = function (frm) {
	const ensureSaved = frm.is_new() || frm.is_dirty() ? frm.save() : Promise.resolve();

	return ensureSaved
		.then(() =>
			sync.helpers.callApi("test_sync_partner", { sync_partner_name: frm.doc.name }, { freeze_message: "Testing connection…" })
		)
		.then((response) => {
			const payload = response?.message;
			const message =
				typeof payload === "string"
					? payload
					: `<pre>${frappe.utils.escape_html(JSON.stringify(payload || {}, null, 2))}</pre>`;

			const persistResult = sync.helpers.persistDocValues("Sync Partner", frm.doc.name, {
				last_connection_status: payload?.status === "ok" ? "Success" : "Error",
				last_checked_on: frappe.datetime.now_datetime(),
				last_connection_error: payload?.status === "ok" ? "" : payload?.message || "",
			});

			return persistResult.then(() =>
				frm.reload_doc().then(() => {
					frappe.msgprint({
						title: __("Connection Test"),
						message,
						indicator: payload?.status === "error" ? "red" : "green",
					});
				})
			);
		})
		.catch((error) => frappe.msgprint(sync.helpers.extractApiErrorMessage(error) || "Connection test failed"));
};

sync.helpers.toggleSourceFields = function (frm) {
	const hasReadQuery = Boolean(sync.helpers.getDefinitionSourceReadQuery(frm));

	sync.helpers.toggleDefinitionField(frm, "read_query", true, false);
	sync.helpers.toggleDefinitionField(frm, "table_name", true, true);
	sync.helpers.setDefinitionFieldProperty(
		frm,
		"table_name",
		"description",
		hasReadQuery
			? __("Writable partner table. Required for inserts, updates, deletes, and column introspection. Read Query is active, so reads are limited by the query while writes still target Table Name.")
			: __("Writable partner table. Required for inserts, updates, deletes, and column introspection. Leave Read Query blank to read the full table.")
	);
	sync.helpers.setDefinitionFieldProperty(
		frm,
		"one_way_match_mode",
		"description",
		__(
			"For A->B and A<-B only: choose whether one source record updates only one matched target or all matched targets."
		)
	);

	sync.helpers.toggleDefinitionField(frm, "match_fields", true, true);
};

sync.helpers.toggleSyncTypeSections = function (frm) {
	const direction = (frm.doc.sync_type || "").toLowerCase();
	const frappeVisible = direction !== "a<-b";
	const partnerVisible = direction !== "a->b";
	const oneWayVisible = direction === "a->b" || direction === "a<-b";
	frm.toggle_display("frappe_modified_field_rows", frappeVisible);
	frm.toggle_display("partner_modified_field_rows", partnerVisible);
	frm.toggle_display("one_way_match_mode", oneWayVisible);
	frm.set_df_property("frappe_modified_field_rows", "description", frappeVisible ? __("Fields used to detect changes on Frappe side.") : "");
	frm.set_df_property("partner_modified_field_rows", "description", partnerVisible ? __("Fields used to detect changes on the partner side.") : "");
};

sync.helpers.togglePartnerFields = function (frm) {
	const type = (frm.doc.partner_type || "").toLowerCase();
	const isMssql = type === "mssql";
	const isFirebird = type === "firebird";
	frm.toggle_display("trust_server_certificate", isMssql);
	frm.toggle_display("charset", isFirebird);
};

sync.helpers.openLatestRun = function (frm) {
	const filters = [
		["Sync Run", "sync_definition", "=", frm.doc.name],
		["Sync Run", "docstatus", "=", 0],
	];
	sync.helpers
		.getList("Sync Run", {
			fields: ["name"],
			filters,
			order_by: "creation desc",
			limit: 1,
		})
		.then((runs) => {
			if (!runs.length) {
				frappe.msgprint(__("No runs found yet."));
				return;
			}
			frappe.set_route("Form", "Sync Run", runs[0].name);
		});
};
