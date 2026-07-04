frappe.ui.form.on('EFRIS Settings', {
    refresh: function(frm) {

        frm.add_custom_button(
            __('Test Connection'),
            function() {
                frappe.call({
                    method: 'efris.efris.doctype.efris_settings.efris_settings.test_connection',
                    freeze: true,
                    freeze_message: __('Testing EFRIS server connection...'),
                    callback: function(response) {
                        if (response.message && response.message.success) {
                            frappe.msgprint({
                                title: __('EFRIS Connection Test'),
                                indicator: 'green',
                                message: __('Server time: {0}', [response.message.server_time]),
                            });
                        }
                    }
                });
            },
            __('Actions')
        );

        frm.add_custom_button(
            __('Refresh AES Key'),
            function() {
                frappe.call({
                method: 'efris.efris.background_tasks.efris_key_manager.test_efris_complete_flow',
                args: {},
                callback: function(response) {
                    if (response.message && response.message.success) {
                        frm.reload_doc();
                    }
                }
            });
            },
            __('Actions')
        );

    }
});
