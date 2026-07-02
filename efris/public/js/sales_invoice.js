frappe.ui.form.on('Sales Invoice', {
    refresh: function(frm) {
        frm.add_custom_button(__('Send Invoice'), function() {
            frappe.call({
                method: 'efris.efris.custom_scripts.upload_invoice.on_send',
                args: {
                    invoice_name: frm.doc.name
                },
                freeze: true,
                freeze_message: __('Sending invoice to EFRIS...'),
                callback: function(response) {
                    if (response.message && response.message.success) {
                        frappe.show_alert({
                            message: __('Invoice sent to EFRIS'),
                            indicator: 'green'
                        });
                        frm.reload_doc();
                    }
                }
            });
        }).addClass('btn-primary');
    }
});
