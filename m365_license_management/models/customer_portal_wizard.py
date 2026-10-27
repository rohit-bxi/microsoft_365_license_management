# -*- coding: utf-8 -*-
"""Grant portal access to the tenant customers (Tenant Customers > Action).

The portal wizard of the Odoo contacts (``portal.wizard``) for ``customer.customer``:
each customer or contact gets a portal user (``portal_user_id``), invited by email to
set its password. Like every Odoo user, the portal user has its own contact
(res.partner), created with it; the customer itself stays independent of the contacts.
"""
from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import email_normalize


class CustomerPortalWizard(models.TransientModel):
    _name = 'customer.portal.wizard'
    _description = 'Grant Portal Access to Tenant Customers'

    def _default_customer_ids(self):
        """The selected customers and their contacts."""
        if self.env.context.get('active_model') != 'customer.customer':
            return []
        customers = self.env['customer.customer'].browse(self.env.context.get('active_ids'))
        return [Command.set((customers | customers.child_ids).ids)]

    customer_ids = fields.Many2many('customer.customer', string="Customers",
                                    default=_default_customer_ids)
    user_ids = fields.One2many('customer.portal.wizard.user', 'wizard_id', string="Users",
                               compute='_compute_user_ids', store=True, readonly=False)
    welcome_message = fields.Text(
        string="Invitation Message",
        help="This text is included in the email sent to new users of the portal.")

    @api.depends('customer_ids')
    def _compute_user_ids(self):
        for wizard in self:
            wizard.user_ids = [Command.create({'customer_id': customer.id,
                                               'email': customer.email})
                               for customer in wizard.customer_ids]

    @api.model
    def action_open_wizard(self):
        """Create the wizard before opening it: the buttons of its lines need saved lines."""
        return self.create({})._action_open_modal()

    def _action_open_modal(self):
        """Keep the wizard open after the action of a line."""
        return {
            'name': _("Portal Access Management"),
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }


class CustomerPortalWizardUser(models.TransientModel):
    _name = 'customer.portal.wizard.user'
    _description = 'Tenant Customer Portal User'

    wizard_id = fields.Many2one('customer.portal.wizard', string="Wizard", required=True,
                                ondelete='cascade')
    customer_id = fields.Many2one('customer.customer', string="Customer", required=True,
                                  readonly=True, ondelete='cascade')
    email = fields.Char(string="Email")
    user_id = fields.Many2one(related='customer_id.portal_user_id', string="User")
    login_date = fields.Datetime(related='user_id.login_date', string="Latest Authentication")
    is_portal = fields.Boolean(string="Is Portal", compute='_compute_group_details')
    is_internal = fields.Boolean(string="Is Internal", compute='_compute_group_details')
    email_state = fields.Selection([
        ('ok', 'Valid'),
        ('ko', 'Invalid'),
        ('exist', 'Already Registered'),
    ], string="Status", compute='_compute_email_state', default='ok')

    @api.depends('email', 'user_id')
    def _compute_email_state(self):
        emails = {line: email_normalize(line.email) for line in self}
        users = self.env['res.users'].sudo().with_context(active_test=False).search_read(
            [('login', 'in', [email for email in emails.values() if email])], ['login'])
        user_by_login = {user['login']: user['id'] for user in users}
        for line, email in emails.items():
            if not email:
                line.email_state = 'ko'
            elif user_by_login.get(email, line.user_id.id) != line.user_id.id:
                line.email_state = 'exist'
            else:
                line.email_state = 'ok'

    @api.depends('user_id', 'user_id.active', 'user_id.group_ids')
    def _compute_group_details(self):
        for line in self:
            user = line.user_id.sudo()
            # A user archived while internal gets the portal access in the settings
            line.is_internal = bool(user) and user._is_internal()
            line.is_portal = bool(user) and user.active and user._is_portal()

    def action_grant_access(self):
        """Create (or reactivate) the portal user of the customer and invite it by email."""
        self.ensure_one()
        self._assert_user_email_uniqueness()
        if self.is_portal or self.is_internal:
            raise UserError(_("%s already has the portal access.", self.customer_id.name))
        self._update_email()
        user_sudo = self.user_id.sudo()
        if not user_sudo:
            user_sudo = self._create_user()
            self.customer_id.portal_user_id = user_sudo
        # Users whose access was revoked used to be in the public group
        user_sudo.write({'active': True, 'group_ids': [
            Command.link(self.env.ref('base.group_portal').id),
            Command.unlink(self.env.ref('base.group_public').id)]})
        self._send_email()
        return self.action_refresh_modal()

    def action_revoke_access(self):
        """Archive the portal user: it can no longer sign in."""
        self.ensure_one()
        if not self.is_portal:
            raise UserError(_("%s has no portal access or is an internal user.",
                              self.customer_id.name))
        self._update_email()
        user_sudo = self.user_id.sudo()
        # The invitation link can no longer be used
        user_sudo.partner_id.signup_type = None
        user_sudo.active = False
        return self.action_refresh_modal()

    def action_invite_again(self):
        """Send the invitation email again."""
        self.ensure_one()
        self._assert_user_email_uniqueness()
        if not self.is_portal:
            raise UserError(_("Grant the portal access to %s first.", self.customer_id.name))
        self._update_email()
        self._send_email()
        return self.action_refresh_modal()

    def action_refresh_modal(self):
        """Keep the wizard open (also the action of the email status icons)."""
        return self.wizard_id._action_open_modal()

    def _create_user(self):
        """Portal user of the customer, in the company of the customer."""
        customer = self.customer_id
        company = customer.company_id or self.env.company
        email = email_normalize(self.email)
        vals = {
            'name': customer.name,
            'login': email,
            'email': email,
            'company_id': company.id,
            'company_ids': [Command.set(company.ids)],
        }
        if customer.lang:
            vals['lang'] = customer.lang
        return self.env['res.users'].sudo().with_company(company).with_context(
            no_reset_password=True)._create_user_from_template(vals)

    def _send_email(self):
        """Invitation to set the password, as in the portal wizard of the contacts."""
        self.ensure_one()
        user_sudo = self.user_id.sudo()
        user_sudo.partner_id.signup_prepare()
        # The signup link of the email needs write access on the contacts, which Microsoft
        # 365 administrators may not have (the portal wizard of the contacts assumes it)
        template = self.env.ref('auth_signup.portal_set_password_email').sudo()
        template.with_context(
            dbname=self.env.cr.dbname, lang=user_sudo.lang,
            welcome_message=self.wizard_id.welcome_message, medium='portalinvite',
        ).send_mail(user_sudo.id, force_send=True)

    def _assert_user_email_uniqueness(self):
        self.ensure_one()
        if self.email_state == 'ko':
            raise UserError(_("%s has no valid email address.", self.customer_id.name))
        if self.email_state == 'exist':
            raise UserError(_("The email address of %s is already used by another user.",
                              self.customer_id.name))

    def _update_email(self):
        """Save a new valid email on the customer and its portal user."""
        email = email_normalize(self.email)
        if self.email_state != 'ok' or email == email_normalize(self.customer_id.email):
            return
        self.customer_id.email = email
        if self.user_id:
            self.user_id.sudo().email = email
