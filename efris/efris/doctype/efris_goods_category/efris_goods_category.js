// Copyright (c) 2026, Othieno Benedict Ernest and contributors
// For license information, please see license.txt

frappe.ui.form.on('EFRIS Goods Category', {
    refresh(frm) {
        if (!frappe.user.has_role('System Manager')) {
            return;
        }

        frm.add_custom_button(__('Sync from EFRIS'), () => {
            frappe.call({
                method: 'efris.efris.doctype.efris_goods_category.efris_goods_category.enqueue_goods_category_sync',
                callback(response) {
                    if (response.message?.success) {
                        frappe.show_alert({
                            message: response.message.message,
                            indicator: 'blue'
                        });
                    }
                }
            });
        });
    }
});
