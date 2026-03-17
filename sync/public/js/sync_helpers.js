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

sync.helpers.runSyncDefinition = function (frm, trigger = "manual") {
	const message = `Running ${frm.doc.name}`;
	sync.helpers.callApi("run_sync_definition", { sync_definition_name: frm.doc.name, trigger })
		.then((response) => {
			if (!response || !response.message) {
				frappe.show_alert({ message, indicator: "green" });
				return;
			}
			frappe.show_alert({ message: response.message, indicator: "green" });
		})
		.catch((error) => frappe.msgprint(error?.message ?? "Unable to run sync definition"));
};

sync.helpers.previewSyncDefinition = function (frm) {
	const limit = frm.doc.preview_limit || 50;
	sync.helpers.callApi("preview_sync_definition", { sync_definition_name: frm.doc.name, limit }, { freeze_message: "Preparing preview…" })
		.then((response) => {
			const payload = response?.message;
			frappe.msgprint({
				title: __("Sync Preview"),
				message:
					typeof payload === "string"
						? payload
						: `<pre>${frappe.utils.escape_html(JSON.stringify(payload || {}, null, 2))}</pre>`,
				indicator: "blue",
			});
		})
		.catch((error) => frappe.msgprint(error?.message ?? "Unable to generate preview"));
};

sync.helpers.exportDefinitionYaml = function (frm) {
	sync.helpers.callApi("export_sync_definition_yaml", { sync_definition_name: frm.doc.name }, { freeze_message: "Generating YAML…" })
		.then((response) => {
			const yaml = response?.message || "";
			if (!yaml) {
				frappe.msgprint(__("No YAML returned."));
				return;
			}
			frappe.prompt(
				{
					fieldtype: "Text Editor",
					fieldname: "yaml",
					label: __("YAML Export"),
					description: __("Copy the YAML below to archive or share."),
					options: yaml,
				},
				() => {},
				__("YAML Export"),
				__("Close")
			);
		})
		.catch((error) => frappe.msgprint(error?.message ?? "Unable to export YAML"));
};

sync.helpers.importDefinitionYaml = function (frm) {
	frappe.prompt(
		[
			{
				fieldtype: "Text Editor",
				fieldname: "yaml",
				label: __("YAML Definition"),
				description: __("Paste a YAML export of a sync definition to import."),
				reqd: 1,
			},
			{
				fieldtype: "Check",
				fieldname: "overwrite",
				label: __("Overwrite existing"),
				description: __("Replace the existing definition if the name already exists."),
			},
		],
		({ yaml, overwrite }) => {
			sync.helpers
				.callApi(
					"import_sync_definition_yaml",
					{
						yaml_payload: yaml,
						overwrite,
					},
					{ freeze_message: "Importing YAML…" }
				)
				.then((response) => {
					frappe.msgprint({
						title: __("Import complete"),
						message: response?.message || __("Sync definition imported."),
						indicator: "green",
					});
					frm.reload_doc();
				})
				.catch((error) => frappe.msgprint(error?.message ?? "Unable to import YAML"));
		},
		__("Import Sync Definition"),
		__("Import")
	);
};

sync.helpers.testPartnerConnection = function (frm) {
	sync.helpers
		.callApi("test_sync_partner", { sync_partner_name: frm.doc.name }, { freeze_message: "Testing connection…" })
		.then((response) => {
			const payload = response?.message;
			const message =
				typeof payload === "string"
					? payload
					: `<pre>${frappe.utils.escape_html(JSON.stringify(payload || {}, null, 2))}</pre>`;
			frappe.msgprint({
				title: __("Connection Test"),
				message,
				indicator: payload?.status === "error" ? "red" : "green",
			});
		})
		.catch((error) => frappe.msgprint(error?.message ?? "Connection test failed"));
};

sync.helpers.toggleSourceFields = function (frm) {
	const usesQuery = Boolean(frm.doc.query);
	frm.toggle_display("query", true);
	frm.toggle_display("table_name", !usesQuery);
	frm.toggle_reqd("table_name", !usesQuery);
	frm.toggle_reqd("query", usesQuery);
};

sync.helpers.toggleSyncTypeSections = function (frm) {
	const direction = (frm.doc.sync_type || "").toLowerCase();
	const frappeVisible = direction !== "a<-b";
	const partnerVisible = direction !== "a->b";
	frm.toggle_display("frappe_modified_fields", frappeVisible);
	frm.toggle_display("partner_modified_fields", partnerVisible);
	frm.set_df_property("frappe_modified_fields", "description", frappeVisible ? __("Fields used to detect changes on Frappe side.") : "");
	frm.set_df_property("partner_modified_fields", "description", partnerVisible ? __("Fields used to detect changes on the partner side.") : "");
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
	frappe.call({
		method: "frappe.client.get_list",
		args: {
			doctype: "Sync Run",
			fields: ["name"],
			filters,
			order_by: "creation desc",
			limit_page_length: 1,
		},
		callback: (response) => {
			const runs = response?.message || [];
			if (!runs.length) {
				frappe.msgprint(__("No runs found yet."));
				return;
			}
			frappe.set_route("Form", "Sync Run", runs[0].name);
		},
	});
};
