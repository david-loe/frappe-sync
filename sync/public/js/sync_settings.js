frappe.provide("sync.settings");

frappe.ui.form.on("Sync Settings", {
	refresh(frm) {
		sync.settings.setupButtons(frm);
	},
});

sync.settings.setupButtons = function (frm) {
	frm.add_custom_button(__("Export YAML"), () => {
		sync.helpers.promptDefinitionYamlExport();
	});
	frm.add_custom_button(__("Import YAML"), () => {
		sync.helpers.importDefinitionYaml(frm);
	});
};
