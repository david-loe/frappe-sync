frappe.provide("sync.partner");

frappe.ui.form.on("Sync Partner", {
	refresh(frm) {
		sync.partner.setupButtons(frm);
		sync.partner.refreshFormState(frm);
	},
	partner_type(frm) {
		sync.partner.refreshFormState(frm);
	},
	auth_type(frm) {
		sync.partner.refreshAuthState(frm);
	},
});

sync.partner.getPartnerType = function (frm) {
	return (frm.doc.partner_type || "").toLowerCase();
};

sync.partner.getAuthType = function (frm) {
	return (frm.doc.auth_type || "Password").toLowerCase();
};

sync.partner.setupButtons = function (frm) {
	frm.add_custom_button(__("Test Connection"), () => {
		sync.helpers.testPartnerConnection(frm);
	});
};

sync.partner.refreshFormState = function (frm) {
	sync.helpers.togglePartnerFields(frm);
	sync.partner.updateConnectionHints(frm);
	sync.partner.updateAuthFields(frm);
	sync.partner.updateStatusHints(frm);
};

sync.partner.refreshAuthState = function (frm) {
	sync.partner.updateAuthFields(frm);
	sync.partner.updateStatusHints(frm);
};

sync.partner.updateConnectionHints = function (frm) {
	const type = sync.partner.getPartnerType(frm);
	const isMssql = type === "mssql";
	const isFirebird = type === "firebird";
	const isPostgres = type === "postgres";

	frm.set_df_property(
		"partner_type",
		"description",
		type === "mssql"
			? __("Selects the SQL Server connector family and its ODBC-specific hints.")
			: type === "postgres"
				? __("Selects the PostgreSQL connector family and its libpq-specific hints.")
				: type === "firebird"
					? __("Selects the Firebird connector family and its charset-specific hints.")
					: __("Selects the connector family and drives the visible connection hints below.")
	);
	frm.set_df_property("host", "label", isMssql ? __("Server") : __("Host"));
	frm.set_df_property(
		"host",
		"description",
		isMssql
			? __("SQL Server host name or instance address.")
			: __("Hostname or IP address of the remote database server.")
	);
	frm.set_df_property(
		"port",
		"description",
		isMssql
			? __("SQL Server port. Leave blank to use the connector default.")
			: isFirebird
				? __("Firebird port. Leave blank to use the default 3050.")
				: __("Database port. Leave blank to use the connector default.")
	);
	frm.set_df_property("database_name", "label", __("Database"));
	frm.set_df_property(
		"database_name",
		"description",
		isMssql
			? __("SQL Server database name.")
			: isFirebird
				? __("Firebird database path or alias.")
				: __("Database name used by the connector.")
	);
	frm.set_df_property(
		"connection_options",
		"description",
		isMssql
			? __("Optional SQL Server / ODBC connection options, one per line or JSON.")
			: isPostgres
				? __("Optional PostgreSQL / libpq connection options, one per line or JSON.")
				: isFirebird
					? __("Optional Firebird driver options, one per line or JSON.")
					: __("Optional connector-specific options, one per line or JSON.")
	);
	frm.set_df_property(
		"time_zone",
		"description",
		__(
			"Optional IANA time zone used when the partner sends naive datetimes. Leave blank only when partner timestamps already carry offsets or already match the site time zone."
		)
	);
	if (isFirebird) {
		frm.set_df_property(
			"charset",
			"description",
			__("Firebird usually expects an explicit charset. UTF8 is a safe default.")
		);
	} else {
		frm.set_df_property("charset", "description", "");
	}
	if (isMssql) {
		frm.set_df_property(
			"trust_server_certificate",
			"description",
			__("Enable only for trusted internal certificates or self-signed SQL Server endpoints.")
		);
	} else {
		frm.set_df_property("trust_server_certificate", "description", "");
	}
	frm.toggle_reqd("host", true);
	frm.toggle_reqd("database_name", true);
	frm.set_df_property(
		"secret_fields",
		"description",
		__("One field name per line. These fields are masked in exports and redaction helpers.")
	);
	frm.set_df_property(
		"connection_notes",
		"description",
		__("Document setup constraints, special driver notes, and what operators need to know before running sync.")
	);
};

sync.partner.updateAuthFields = function (frm) {
	const type = sync.partner.getAuthType(frm);
	const partnerType = sync.partner.getPartnerType(frm);
	const showPassword = type === "password";
	const showApiKey = type === "api key";
	const showCertificate = type === "certificate";
	frm.toggle_display("username", showPassword);
	frm.toggle_display("password", showPassword);
	frm.toggle_display("api_key", showApiKey);
	frm.toggle_display("api_secret", showApiKey);
	frm.toggle_display("certificate_path", showCertificate);
	frm.toggle_reqd("username", showPassword);
	frm.toggle_reqd("password", showPassword);
	frm.toggle_reqd("api_key", showApiKey);
	frm.toggle_reqd("api_secret", showApiKey);
	frm.toggle_reqd("certificate_path", showCertificate);
	frm.set_df_property(
		"auth_type",
		"description",
		type === "api key"
			? __("Use API credentials when the target system exposes key-based access.")
			: type === "certificate"
				? __("Use a certificate path when the connector authenticates through client certificates.")
				: type === "none"
					? partnerType === "mssql"
						? __("Use this when SQL Server authentication is handled externally or through the connection string.")
						: __("Use this when credentials are injected externally or no authentication is needed.")
					: partnerType === "mssql"
						? __("Use SQL login credentials when the SQL Server connection expects them.")
						: __("Use standard username/password authentication.")
	);
	frm.set_df_property(
		"username",
		"description",
		showPassword
			? partnerType === "mssql"
				? __("SQL login or database user used for password-based authentication.")
				: __("Username used by the connector for password-based authentication.")
			: ""
	);
	frm.set_df_property(
		"password",
		"description",
		showPassword
			? partnerType === "mssql"
				? __("Password paired with the SQL login used for password-based authentication.")
				: __("Password used by the connector for password-based authentication.")
			: ""
	);
	frm.set_df_property(
		"api_key",
		"description",
		showApiKey ? __("API key or access token provided by the upstream system.") : ""
	);
	frm.set_df_property(
		"api_secret",
		"description",
		showApiKey ? __("Secret value paired with the API key.") : ""
	);
	frm.set_df_property(
		"certificate_path",
		"description",
		showCertificate ? __("Filesystem path to the client certificate used for authentication.") : ""
	);
};

sync.partner.updateFieldHints = sync.partner.updateConnectionHints;
sync.partner.toggleAuthFields = sync.partner.updateAuthFields;

sync.partner.updateStatusHints = function (frm) {
	const status = (frm.doc.last_connection_status || "").toLowerCase();
	const hasCheckedOn = Boolean(frm.doc.last_checked_on);
	const hasError = Boolean(frm.doc.last_connection_error);
	frm.set_df_property("last_connection_status", "label", __("Status"));
	frm.set_df_property("last_checked_on", "label", __("Checked On"));
	frm.set_df_property("last_connection_error", "label", __("Latest Error"));

	frm.set_df_property(
		"last_connection_status",
		"description",
		status === "success"
			? __("The latest recorded connection test succeeded. Use Test Connection to refresh it.")
			: status === "error"
				? __("The latest recorded connection test failed. Use Test Connection to refresh it.")
				: __("No connection test has been recorded yet. Use Test Connection to record one.")
	);
	frm.set_df_property(
		"last_checked_on",
		"description",
		hasCheckedOn
			? __("Timestamp of the latest recorded connection test.")
			: __("No connection test has been recorded yet.")
	);
	frm.set_df_property(
		"last_connection_error",
		"description",
		hasError
			? __("Most recent failure details from the latest connection test. Earlier errors are not retained here.")
			: __("No connection error has been recorded yet.")
	);
};
