const EFRIS_ITEM_UOMS = ['PP-Piece', 'Pair', 'Litre'];

function showEfrisItemSyncDialog(frm) {
    const dialog = new frappe.ui.Dialog({
        title: __('Sync Item with EFRIS'),
        fields: [
            {
                fieldname: 'goods_name',
                fieldtype: 'Data',
                label: __('EFRIS Goods Name'),
                reqd: 1,
                default: frm.doc.custom_goods_service_name || frm.doc.item_name
            },
            {
                fieldname: 'category_id',
                fieldtype: 'Data',
                label: __('Goods Category ID'),
                reqd: 1,
                default: frm.doc.custom_goods_category_id || ''
            },
            {
                fieldname: 'efris_uom',
                fieldtype: 'Select',
                label: __('EFRIS Unit of Measure'),
                options: EFRIS_ITEM_UOMS.join('\n'),
                reqd: 1,
                default: frm.doc.custom_uom_code_efris || 'PP-Piece'
            },
            {
                fieldname: 'unit_price',
                fieldtype: 'Currency',
                label: __('EFRIS Unit Price'),
                options: 'UGX',
                reqd: 1,
                default: Number(String(frm.doc.custom_efris_price || frm.doc.standard_rate || 0).replace(/,/g, ''))
            },
            {
                fieldname: 'vat',
                fieldtype: 'Select',
                label: __('VAT'),
                options: ['0.18', '0', '-'].join('\n'),
                reqd: 1,
                default: frm.doc.custom_vat_ || '0.18'
            }
        ],
        primary_action_label: __('Sync with EFRIS'),
        primary_action(values) {
            dialog.hide();
            frappe.call({
                method: 'efris.efris.custom_scripts.item_sync.sync_item_with_efris',
                args: {
                    item_name: frm.doc.name,
                    goods_name: values.goods_name,
                    category_id: values.category_id,
                    efris_uom: values.efris_uom,
                    unit_price: values.unit_price,
                    vat: values.vat
                },
                freeze: true,
                freeze_message: __('Syncing item with EFRIS...'),
                callback(response) {
                    if (!response.message?.success) {
                        return;
                    }

                    frappe.show_alert({
                        message: response.message.message,
                        indicator: 'green'
                    });
                    frm.reload_doc();
                }
            });
        }
    });

    dialog.show();
}

frappe.ui.form.on('Item', {
    refresh(frm) {
        if (frm.is_new() || !frm.perm?.[0]?.write) {
            return;
        }

        frm.add_custom_button(__('Sync with EFRIS'), () => {
            showEfrisItemSyncDialog(frm);
        }).addClass('btn-primary');
    }
});
