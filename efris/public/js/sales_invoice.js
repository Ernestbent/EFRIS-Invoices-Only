const EFRIS_SEND_INVOICE_ALLOWED_USERS = [
    'ernestben69@gmail.com',
    'reports@autozonepro.org'
];

function getEfrisErrorMessage(error) {
    const responseJSON = error?.responseJSON || error?.xhr?.responseJSON || {};
    const serverMessages = responseJSON._server_messages;

    if (serverMessages) {
        try {
            const parsedMessages = JSON.parse(serverMessages);
            if (Array.isArray(parsedMessages) && parsedMessages.length) {
                const firstMessage = JSON.parse(parsedMessages[0]);
                return firstMessage.message || firstMessage;
            }
        } catch (e) {
            // Fall through to other error formats.
        }
    }

    return (
        responseJSON.exception ||
        responseJSON.message ||
        error?.message ||
        error?.exc_type ||
        __('The invoice was not submitted to EFRIS. Please check the latest Integration Request or Error Log.')
    );
}

frappe.ui.form.on('Sales Invoice', {
    refresh: function(frm) {
        if (frm.doc.custom_efris_synced) {
            return;
        }

        if (!EFRIS_SEND_INVOICE_ALLOWED_USERS.includes(frappe.session.user)) {
            return;
        }

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
                            message: __('Invoice submitted to EFRIS'),
                            indicator: 'green'
                        });
                        frm.reload_doc();
                    } else {
                        frappe.msgprint({
                            title: __('EFRIS Send Failed'),
                            indicator: 'red',
                            message: __('The invoice was not submitted to EFRIS. Please check the latest Integration Request or Error Log.')
                        });
                    }
                },
                error: function(error) {
                    frappe.msgprint({
                        title: __('EFRIS Send Failed'),
                        indicator: 'red',
                        message: getEfrisErrorMessage(error)
                    });
                }
            });
        }).addClass('btn-primary');
    }
});
