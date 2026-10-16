# -*- coding: utf-8 -*-
from odoo import fields, models


class M365Product(models.Model):
    """Product catalogue master.

    Microsoft Graph only returns the technical ``skuPartNumber``
    (e.g. ``O365_BUSINESS_PREMIUM``). This master maps it to the friendly
    product name shown on the dashboard. Unknown part numbers discovered during
    synchronisation are created automatically and can be renamed here.
    """
    _name = 'm365.product'
    _description = 'Microsoft 365 Product Catalogue'
    _order = 'category, name'

    name = fields.Char(string="Product Name", required=True, translate=False)
    sku_part_number = fields.Char(string="SKU Part Number", required=True, index=True,
                                  help="Technical name returned by Microsoft Graph "
                                       "(skuPartNumber), e.g. BUSINESS_STANDARD.")
    category = fields.Selection([
        ('microsoft365', 'Microsoft 365 / Office 365'),
        ('exchange', 'Exchange Online'),
        ('sharepoint', 'SharePoint / OneDrive'),
        ('teams', 'Teams & Voice'),
        ('power', 'Power Platform / Power BI'),
        ('project_visio', 'Project & Visio'),
        ('security', 'Security & Identity'),
        ('other', 'Other'),
    ], string="Category", default='other', required=True)
    active = fields.Boolean(default=True)
    auto_created = fields.Boolean(string="Auto Created", readonly=True,
                                  help="Created automatically during synchronisation.")
    note = fields.Text(string="Notes")
    sku_ids = fields.One2many('m365.sku', 'product_id', string="Tenant Subscriptions")
    sku_count = fields.Integer(compute='_compute_sku_count', string="Subscriptions")

    _sku_part_number_uniq = models.Constraint(
        'unique(sku_part_number)',
        'This SKU part number already exists in the product catalogue.',
    )

    def _compute_sku_count(self):
        for rec in self:
            rec.sku_count = len(rec.sku_ids)
