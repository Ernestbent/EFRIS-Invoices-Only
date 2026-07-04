frappe.ui.form.on('Sales Invoice', {
    refresh: function(frm) {
        if (frm.doc.custom_efris_synced) {
            return;
        }

        frm.add_custom_button(__('Send Invoice'), function() {
            frappe.call({
                method: 'efris.efris.custom_scripts.upload_invoice.on_send',
                args: {
                    invoice_name: frm.doc.name
                },
                callback: function(response) {
                    if (response.message && response.message.success) {
                        frappe.show_alert({
                            message: __('Invoice queued for EFRIS submission'),
                            indicator: 'green'
                        });
                    }
                }
            });
        }).addClass('btn-primary');
    }
});
