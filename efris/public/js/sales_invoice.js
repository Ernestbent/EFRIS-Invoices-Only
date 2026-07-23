const EFRIS_SEND_INVOICE_USER = 'reports@autozonepro.org';

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

function updateStockDifference(row) {
    const efrisQty = Number(row.custom_efris_qty || 0);
    const allWarehousesQty = Number(row.custom_containers_qty || 0);
    row.custom_diffefris__main = efrisQty - allWarehousesQty;
}

function fetchEfrisStockForRow(frm, cdt, cdn) {
    const row = locals[cdt][cdn];
    if (!row || !row.item_code) {
        if (row) {
            row.custom_efris_qty = 0;
            row.custom_containers_qty = 0;
            row.custom_diffefris__main = 0;
            frm.refresh_field('items');
        }
        return;
    }

    frappe.call({
        method: 'efris.efris.custom_scripts.efris_stock_ledger.get_sales_invoice_item_efris_stock',
        args: {
            item_code: row.item_code,
            efris_product_code: row.custom_efris_product_code || ''
        },
        callback: function(response) {
            const result = response.message || {};
            row.custom_efris_qty = Number(result.efris_qty || 0);
            row.custom_containers_qty = Number(result.all_warehouses_qty || 0);
            updateRowStockFields(frm, cdt, cdn);
            frm.refresh_field('items');
            scheduleRowStockDifferenceRefresh(frm, cdt, cdn);
        }
    });
}

function fetchAllEfrisStockForRows(frm) {
    const items = (frm.doc.items || [])
        .filter((row) => row.item_code)
        .map((row) => ({
            row_name: row.name,
            item_code: row.item_code,
            efris_product_code: row.custom_efris_product_code || ''
        }));

    if (!items.length) {
        return;
    }

    frappe.call({
        method: 'efris.efris.custom_scripts.efris_stock_ledger.get_sales_invoice_efris_stock_rows',
        args: {
            invoice_name: frm.is_new() ? '' : frm.doc.name,
            items: JSON.stringify(items)
        },
        callback: function(response) {
            const stockRows = response.message?.rows || {};

            (frm.doc.items || []).forEach((row) => {
                const stock = stockRows[row.name] || {};
                row.custom_efris_qty = Number(stock.efris_qty || 0);
                row.custom_containers_qty = Number(stock.all_warehouses_qty || 0);
                updateStockDifference(row);
            });

            frm.refresh_field('items');
        }
    });
}

function updateRowStockFields(frm, cdt, cdn) {
    const row = locals[cdt][cdn];
    if (!row) {
        return;
    }

    const efrisQty = Number(row.custom_efris_qty || 0);
    const allWarehousesQty = Number(row.custom_containers_qty || 0);
    row.custom_diffefris__main = efrisQty - allWarehousesQty;
}

function scheduleRowStockDifferenceRefresh(frm, cdt, cdn) {
    [200, 700, 1500, 3000].forEach((delay) => {
        setTimeout(() => {
            updateRowStockFields(frm, cdt, cdn);
            frm.refresh_field('items');
        }, delay);
    });
}

function refreshAllEfrisStockDifferences(frm) {
    (frm.doc.items || []).forEach((row) => {
        updateRowStockFields(frm, row.doctype, row.name);
    });
}

function sendInvoiceToEfris(
    frm,
    excludedRowNames = [],
    quantityOverrides = {}
) {
    frappe.call({
        method: 'efris.efris.custom_scripts.upload_invoice.on_send',
        args: {
            invoice_name: frm.doc.name,
            excluded_row_names: JSON.stringify(excludedRowNames),
            quantity_overrides: JSON.stringify(quantityOverrides)
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
}

function showEfrisItemSelectionDialog(frm) {
    const invoiceItems = frm.doc.items || [];
    if (!invoiceItems.length) {
        frappe.msgprint(__('Add at least one item before sending the invoice to EFRIS.'));
        return;
    }

    const problemItems = invoiceItems.filter((row) => {
        const difference = Number(row.custom_efris_qty || 0) - Number(row.custom_containers_qty || 0);
        return difference < 0;
    });

    if (!problemItems.length) {
        sendInvoiceToEfris(frm);
        return;
    }

    const escapeHtml = (value) => frappe.utils.escape_html(String(value ?? ''));
    const itemRows = problemItems.map((row) => {
        const invoiceQty = Number(row.qty || 0);
        const efrisQty = Number(row.custom_efris_qty || 0);
        const allWarehousesQty = Number(row.custom_containers_qty || 0);
        const difference = efrisQty - allWarehousesQty;
        const balanceAfterSend = efrisQty - invoiceQty;
        const differenceClass = difference < 0 ? 'text-danger' : 'text-success';
        const balanceAfterSendClass = balanceAfterSend < 0 ? 'text-danger' : 'text-success';

        return `
            <tr data-row-name="${escapeHtml(row.name)}" data-efris-deleted="0">
                <td class="text-center">
                    <input
                        type="checkbox"
                        class="efris-select-item"
                        data-row-name="${escapeHtml(row.name)}"
                    >
                </td>
                <td class="efris-item-label">
                    ${escapeHtml(row.item_code || row.item_name)}
                    <span class="badge badge-danger efris-deleted-label hide">${__('Removed')}</span>
                </td>
                <td>
                    <input
                        type="number"
                        class="form-control input-xs efris-send-qty"
                        data-row-name="${escapeHtml(row.name)}"
                        value="${escapeHtml(invoiceQty)}"
                        min="0"
                        max="${escapeHtml(Math.max(efrisQty, 0))}"
                        step="any"
                    >
                </td>
                <td class="text-right">${format_number(efrisQty)}</td>
                <td class="text-right">${format_number(allWarehousesQty)}</td>
                <td class="text-right ${differenceClass}">${format_number(difference)}</td>
                <td class="text-right efris-balance-after-send ${balanceAfterSendClass}">
                    ${format_number(balanceAfterSend)}
                </td>
            </tr>
        `;
    }).join('');

    function getSubmissionValues(dialog) {
        const excludedRowNames = new Set(
            dialog.$wrapper
                .find('tr[data-efris-deleted="1"]')
                .map((index, row) => row.dataset.rowName)
                .get()
        );
        const quantityOverrides = {};
        let invalidQuantity = false;
        const insufficientStockItems = [];

        dialog.$wrapper.find('.efris-send-qty').each((index, input) => {
            if (input.disabled) {
                return;
            }

            const quantity = Number(input.value);
            const rowName = input.dataset.rowName;

            if (!Number.isFinite(quantity) || quantity < 0) {
                invalidQuantity = true;
                return;
            }

            if (quantity === 0) {
                excludedRowNames.add(rowName);
                return;
            }

            const invoiceRow = invoiceItems.find((row) => row.name === rowName);
            const efrisQuantity = Number(invoiceRow?.custom_efris_qty || 0);
            const itemLabel = invoiceRow?.item_code || invoiceRow?.item_name || rowName;

            if (quantity > efrisQuantity) {
                insufficientStockItems.push(
                    `${itemLabel} (${format_number(quantity)} > ${format_number(efrisQuantity)})`
                );
                return;
            }

            if (!invoiceRow || quantity !== Number(invoiceRow.qty || 0)) {
                quantityOverrides[rowName] = quantity;
            }
        });

        if (invalidQuantity) {
            frappe.msgprint(__('Qty to EFRIS must be zero or a positive number.'));
            return null;
        }

        if (insufficientStockItems.length) {
            frappe.msgprint(
                __('Qty to EFRIS cannot exceed the available EFRIS stock: {0}. Reduce the quantity, set it to zero, or delete the item from this submission.', [
                    insufficientStockItems.join(', ')
                ])
            );
            return null;
        }

        return {
            excludedRowNames: Array.from(excludedRowNames),
            quantityOverrides
        };
    }

    function submitDialog(dialog) {
        const submission = getSubmissionValues(dialog);
        if (!submission) {
            return;
        }

        if (submission.excludedRowNames.length === invoiceItems.length) {
            dialog.hide();
            frappe.msgprint({
                title: __('Nothing Sent'),
                indicator: 'orange',
                message: __('All invoice items have zero quantity or were removed. Nothing was sent to EFRIS.')
            });
            return;
        }

        dialog.hide();
        sendInvoiceToEfris(
            frm,
            submission.excludedRowNames,
            submission.quantityOverrides
        );
    }

    const dialog = new frappe.ui.Dialog({
        title: __('Adjust EFRIS Submission'),
        size: 'large',
        fields: [
            {
                fieldtype: 'HTML',
                fieldname: 'items',
                options: `
                    <p class="text-muted">
                        ${__('These items have a negative EFRIS difference. Adjust Qty to EFRIS so it does not exceed the available EFRIS stock, or remove the item from this submission. These changes apply only to what is sent to URA.')}
                    </p>
                    <style>
                        .efris-row-deleted td:not(:first-child) {
                            background: var(--red-50);
                            opacity: 0.55;
                        }
                        .efris-row-deleted .efris-item-label {
                            text-decoration: line-through;
                        }
                    </style>
                    <div class="table-responsive">
                        <table class="table table-bordered table-hover">
                            <thead>
                                <tr>
                                    <th style="width: 70px">${__('Select')}</th>
                                    <th>${__('Item')}</th>
                                    <th style="width: 120px">${__('Qty to EFRIS')}</th>
                                    <th class="text-right">${__('EFRIS')}</th>
                                    <th class="text-right">${__('All Warehouses')}</th>
                                    <th class="text-right">${__('Difference')}</th>
                                    <th class="text-right">${__('EFRIS After Send')}</th>
                                </tr>
                            </thead>
                            <tbody>${itemRows}</tbody>
                        </table>
                    </div>
                    <button type="button" class="btn btn-danger btn-sm efris-delete-selected">
                        ${__('Remove Selected')}
                    </button>
                `
            }
        ],
        primary_action_label: __('Send to EFRIS'),
        primary_action() {
            submitDialog(dialog);
        }
    });

    dialog.show();
    dialog.$wrapper.on('input', '.efris-send-qty', function() {
        const invoiceRow = invoiceItems.find((row) => row.name === this.dataset.rowName);
        const quantity = Number(this.value || 0);
        const balanceAfterSend = Number(invoiceRow?.custom_efris_qty || 0) - quantity;
        const balanceCell = $(this).closest('tr').find('.efris-balance-after-send');

        balanceCell
            .text(format_number(balanceAfterSend))
            .toggleClass('text-danger', balanceAfterSend < 0)
            .toggleClass('text-success', balanceAfterSend >= 0);
    });
    dialog.$wrapper.on('click', '.efris-delete-selected', function() {
        const selectedItems = dialog.$wrapper.find('.efris-select-item:checked');
        if (!selectedItems.length) {
            frappe.msgprint(__('Select at least one item to remove from the EFRIS submission.'));
            return;
        }

        selectedItems.each((index, checkbox) => {
            const row = $(checkbox).closest('tr');
            row.attr('data-efris-deleted', '1').addClass('efris-row-deleted');
            row.find('.efris-send-qty').prop('disabled', true);
            row.find('.efris-deleted-label').removeClass('hide');
            $(checkbox).prop('checked', false).prop('disabled', true);
        });
    });
}

frappe.ui.form.on('Sales Invoice', {
    refresh: function(frm) {
        fetchAllEfrisStockForRows(frm);

        [400, 1200, 2500].forEach((delay) => {
            setTimeout(() => {
                refreshAllEfrisStockDifferences(frm);
                frm.refresh_field('items');
            }, delay);
        });

        if (frm.doc.custom_efris_synced) {
            return;
        }

        if (frm.is_new() || frm.is_dirty()) {
            return;
        }

        if (frappe.session.user !== EFRIS_SEND_INVOICE_USER) {
            return;
        }

        frm.add_custom_button(__('Send Invoice'), function() {
            showEfrisItemSelectionDialog(frm);
        });
    }
});

frappe.ui.form.on('Sales Invoice Item', {
    item_code: function(frm, cdt, cdn) {
        fetchEfrisStockForRow(frm, cdt, cdn);
        scheduleRowStockDifferenceRefresh(frm, cdt, cdn);
    },
    qty: function(frm, cdt, cdn) {
        scheduleRowStockDifferenceRefresh(frm, cdt, cdn);
    },
    warehouse: function(frm, cdt, cdn) {
        updateRowStockFields(frm, cdt, cdn);
        fetchEfrisStockForRow(frm, cdt, cdn);
        scheduleRowStockDifferenceRefresh(frm, cdt, cdn);
    },
    actual_qty: function(frm, cdt, cdn) {
        updateRowStockFields(frm, cdt, cdn);
        scheduleRowStockDifferenceRefresh(frm, cdt, cdn);
    },
    form_render: function(frm, cdt, cdn) {
        fetchEfrisStockForRow(frm, cdt, cdn);
        scheduleRowStockDifferenceRefresh(frm, cdt, cdn);
    }
});
