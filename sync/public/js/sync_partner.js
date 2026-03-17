frappe.provide("sync.partner");

frappe.ui.form.on("Sync Partner", {
	refresh(frm) {
		sync.partner.setupButtons(frm);
		sync.helpers.togglePartnerFields(frm);
		sync.partner.toggleAuthFields(frm);
		sync.partner.updateFieldHints(frm);
	},
	partner_type(frm) {
		sync.helpers.togglePartnerFields(frm);
		sync.partner.updateFieldHints(frm);
		sync.partner.toggleAuthFields(frm);
	},
	auth_type(frm) {
		sync.partner.toggleAuthFields(frm);
	},
});

sync.partner.setupButtons = function (frm) {
	frm.add_custom_button(__("Test Connection"), () => {
		sync.helpers.testPartnerConnection(frm);
	});
};

sync.partner.updateFieldHints = function (frm) {
	const type = (frm.doc.partner_type || "").toLowerCase();
	if (type === "firebird") {
		frm.set_df_property("charset", "description", __("Firebird expects a charset (default UTF8)."));
	} else if (type === "mssql") {
		frm.set_df_property("trust_server_certificate", "description", __("Enable to skip certificate verification when using self-signed certs."));
	} else {
		frm.set_df_property("charset", "description", "");
		frm.set_df_property("trust_server_certificate", "description", "");
	}
};

sync.partner.toggleAuthFields = function (frm) {
	const type = (frm.doc.auth_type || "Password").toLowerCase();
	const showPassword = type === "password";
	const showApiKey = type === "api key";
	const showCertificate = type === "certificate";
	frm.toggle_display("username", showPassword);
	frm.toggle_display("password", showPassword);
	frm.toggle_display("api_key", showApiKey);
	frm.toggle_display("api_secret", showApiKey);
	frm.toggle_display("certificate_path", showCertificate);
	frm.set_df_property(
		"auth_type",
		"description",
		type === "api key"
			? __("Use API credentials instead of username/password.")
			: type === "certificate"
				? __("Provide a certificate path that the connector can resolve.")
				: __("Use username/password authentication.")
	);
};
