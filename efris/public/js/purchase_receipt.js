function getPurchaseReceiptEfrisError(error) {
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
            // Fall through to the standard response fields.
        }
    }

    return (
        responseJSON.exception ||
        responseJSON.message ||
        error?.message ||
        __('The Purchase Receipt was not synced. Check the latest Integration Request or Error Log.')
    );
}

function syncPurchaseReceiptWithEfris(frm) {
    frappe.call({
        method: 'efris.efris.custom_scripts.purchase_receipt_stock_in.sync_purchase_receipt_with_efris',
        args: {
            purchase_receipt_name: frm.doc.name
        },
        freeze: true,
        freeze_message: __('Syncing Purchase Receipt stock with EFRIS...'),
        callback(response) {
            const result = response.message || {};
            if (!result.success) {
                return;
            }

            frappe.msgprint(result.message);

            if (result.warning) {
                frappe.msgprint({
                    title: __('EFRIS Sync Warning'),
                    indicator: 'orange',
                    message: result.warning
                });
            }

            frm.reload_doc();
        },
        error(error) {
            frappe.msgprint({
                title: __('EFRIS Stock-In Failed'),
                indicator: 'red',
                message: getPurchaseReceiptEfrisError(error)
            });
        }
    });
}

function addPurchaseReceiptEfrisButton(frm) {
    frappe.call({
        method: 'efris.efris.custom_scripts.purchase_receipt_stock_in.get_purchase_receipt_efris_sync_status',
        args: {
            purchase_receipt_name: frm.doc.name
        },
        callback(response) {
            const result = response.message || {};
            if (result.synced) {
                frm.dashboard.add_indicator(__('EFRIS Synced'), 'green');
                return;
            }

            frm.add_custom_button(__('Sync with EFRIS'), () => {
                syncPurchaseReceiptWithEfris(frm);
            }).addClass('btn-primary');
        }
    });
}

frappe.ui.form.on('Purchase Receipt', {
    refresh(frm) {
        if (frm.doc.docstatus !== 1 || frm.doc.is_return || !frm.perm?.[0]?.submit) {
            return;
        }

        addPurchaseReceiptEfrisButton(frm);
    }
});
