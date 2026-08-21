// Copyright (c) 2026, Othieno Benedict Ernest and contributors
// For license information, please see license.txt

frappe.listview_settings['EFRIS Goods Category'] = {
    onload(listview) {
        if (!frappe.user.has_role('System Manager')) {
            return;
        }

        listview.page.add_inner_button(__('Sync from EFRIS'), () => {
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

        frappe.realtime.on('efris_goods_category_sync_complete', (result) => {
            frappe.show_alert({
                message: __('Goods category sync complete: {0} categories processed.', [result.total]),
                indicator: 'green'
            }, 8);
            listview.refresh();
        });
    }
};
