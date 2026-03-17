frappe.provide("sync.forms");

frappe.ui.form.on("Sync Definition", {
	refresh(frm) {
		sync.forms.setupButtons(frm);
		sync.helpers.toggleSourceFields(frm);
		sync.helpers.toggleSyncTypeSections(frm);
	},
	sync_type(frm) {
		sync.helpers.toggleSyncTypeSections(frm);
	},
	query(frm) {
		sync.helpers.toggleSourceFields(frm);
	},
	table_name(frm) {
		sync.helpers.toggleSourceFields(frm);
	},
});

sync.forms.setupButtons = function (frm) {
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
