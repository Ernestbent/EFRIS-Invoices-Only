frappe.ui.form.on('Sales Invoice', {
    refresh: function(frm) {
        const efris_group = __('EFRIS Actions');

        if (frm.doc.docstatus === 0 || frm.doc.docstatus === 1) {
            frm.add_custom_button(__('Fetch EFRIS Prices'), function() {
                frappe.call({
                    method: "efris.efris.custom_scripts.query_stock_first.update_unit_price_from_efris",
                    args: {
                        invoice_name: frm.doc.name
                    },
                    freeze: true,
                    freeze_message: __("Fetching EFRIS unit prices..."),
                    callback: function(r) {
                        if (r.message && r.message.success) {
                            frappe.msgprint({
                                title: __('Prices Updated'),
                                message: __("Updated {0} item(s) with EFRIS unit prices", [r.message.updated]),
                                indicator: 'green'
                            });
                            frm.reload_doc();
                        } else if (r.message) {
                            frappe.msgprint({
                                title: __('Error'),
                                message: r.message.message || __('Failed to fetch prices'),
                                indicator: 'red'
                            });
                        }
                    }
                });
            }, efris_group);
        }

        if (frm.doc.docstatus === 1) {
            frm.add_custom_button(__('Validate Efris Stock'), function() {
                frappe.call({
                    method: "efris.efris.custom_scripts.query_stock_first.validate_invoice_stock_before_efris",
                    args: {
                        invoice_name: frm.doc.name
                    },
                    freeze: true,
                    freeze_message: __("Checking EFRIS stock for all items..."),
                    callback: function(r) {
                        if (r.message && r.message.success) {
                            frappe.msgprint({
                                title: __('Stock Validation Complete'),
                                message: __('All items have sufficient stock in EFRIS!'),
                                indicator: 'green'
                            });
                            frm.reload_doc();
                        } else if (r.message) {
                            frappe.msgprint({
                                title: __('Stock Validation Failed'),
                                message: r.message.details || r.message.message || __('Stock validation failed'),
                                indicator: 'red'
                            });
                        }
                    }
                });
            }, efris_group);
        }
    }
});
