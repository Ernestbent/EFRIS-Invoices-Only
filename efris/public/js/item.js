const EFRIS_ITEM_UOMS = ['PP-Piece', 'Pair', 'Litre'];
const EFRIS_STOCK_UOM_DEFAULTS = {
    piece: 'PP-Piece',
    pieces: 'PP-Piece',
    pair: 'Pair',
    litre: 'Litre',
    liter: 'Litre'
};

function populateMissingEfrisDetails(frm) {
    const values = {};

    if (!frm.doc.custom_goods_service_name && frm.doc.item_name) {
        values.custom_goods_service_name = frm.doc.item_name;
    }

    const stockUom = String(frm.doc.stock_uom || '').trim().toLowerCase();
    if (!frm.doc.custom_uom_code_efris && EFRIS_STOCK_UOM_DEFAULTS[stockUom]) {
        values.custom_uom_code_efris = EFRIS_STOCK_UOM_DEFAULTS[stockUom];
    }

    if (!frm.doc.custom_efris_price && frm.doc.standard_rate) {
        values.custom_efris_price = frm.doc.standard_rate;
    }

    if (Object.keys(values).length) {
        return frm.set_value(values);
    }
}

function getVatFromCategory(category) {
    if (!category) {
        return null;
    }

    if (Number(category.is_exempt) === 1) {
        return '-';
    }

    if (Number(category.is_zero_rate) === 1) {
        return '0';
    }

    const taxRate = String(category.tax_rate || '').replace('%', '').trim();
    if (!taxRate) {
        return null;
    }

    const numericTaxRate = Number(taxRate);
    if (Number.isNaN(numericTaxRate)) {
        return null;
    }

    if (numericTaxRate === 0) {
        return '0';
    }

    if (numericTaxRate === 18 || numericTaxRate === 0.18) {
        return '0.18';
    }

    return null;
}

function getCategoryVat(categoryId, callback) {
    if (!categoryId) {
        callback('');
        return;
    }

    frappe.db.get_value(
        'EFRIS Goods Category',
        categoryId,
        ['tax_rate', 'is_zero_rate', 'is_exempt'],
        (response) => callback(getVatFromCategory(response))
    );
}

function setItemVatFromGoodsCategory(frm) {
    getCategoryVat(frm.doc.custom_goods_category_id, (vat) => {
        if (vat !== null) {
            frm.set_value('custom_vat_', vat);
        }
    });
}

function showEfrisItemSyncDialog(frm) {
    let dialog;
    dialog = new frappe.ui.Dialog({
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
                fieldtype: 'Link',
                options: 'EFRIS Goods Category',
                label: __('Goods Category ID'),
                reqd: 1,
                default: frm.doc.custom_goods_category_id || '',
                get_query() {
                    return {
                        filters: {
                            enabled: 1,
                            is_leaf_node: 1,
                            excisable: 0
                        }
                    };
                },
                onchange() {
                    getCategoryVat(dialog.get_value('category_id'), (vat) => {
                        if (vat !== null) {
                            dialog.set_value('vat', vat);
                        }
                    });
                }
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
            frm.set_value({
                custom_goods_service_name: values.goods_name,
                custom_goods_category_id: values.category_id,
                custom_uom_code_efris: values.efris_uom,
                custom_efris_price: values.unit_price,
                custom_vat_: values.vat
            });
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
    setup(frm) {
        frm.set_query('custom_goods_category_id', () => ({
            filters: {
                enabled: 1,
                is_leaf_node: 1,
                excisable: 0
            }
        }));
    },

    before_save(frm) {
        return populateMissingEfrisDetails(frm);
    },

    custom_goods_category_id(frm) {
        setItemVatFromGoodsCategory(frm);
    },

    refresh(frm) {
        if (frm.is_new() || !frm.perm?.[0]?.write) {
            return;
        }

        frm.add_custom_button(__('Sync with EFRIS'), () => {
            showEfrisItemSyncDialog(frm);
        }).addClass('btn-primary');
    }
});
