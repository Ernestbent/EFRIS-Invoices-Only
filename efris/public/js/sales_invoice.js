frappe.ui.form.on('Sales Invoice', {
    refresh: function(frm) {
        frm.add_custom_button(__('Send Invoice'), function() {
            frappe.call({
                method: 'efris.efris.custom_scripts.upload_invoice.on_send',
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