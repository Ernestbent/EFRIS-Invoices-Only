frappe.ui.form.on('EFRIS Settings', {
    refresh: function(frm) {
        frm.add_custom_button(__('Refresh AES Key'), function() {
            frappe.call({
                method: 'efris.efris.background_tasks.efris_key_manager.test_efris_complete_flow',
                args: {},
                callback: function(response) {
                    if (response.message && response.message.success) {
                        frm.reload_doc();
                    }
                }
            });
        }).addClass('btn-primary');
    }
});