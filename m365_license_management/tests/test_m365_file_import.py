# -*- coding: utf-8 -*-
import base64

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged('post_install', '-at_install')
class TestM365FileImport(TransactionCase):

    def _rows(self, content, filename='file.csv'):
        wizard = self.env['m365.license.bulk.wizard'].create({
            'file': base64.b64encode(content), 'filename': filename})
        return wizard._read_rows()

    def test_cell_text(self):
        Mixin = self.env['m365.file.import.mixin']
        self.assertEqual([Mixin._cell_text(value) for value in
                          (None, True, False, 5.0, 2.5, 7, '  x  ')],
                         ['', 'yes', 'no', '5', '2.5', '7', 'x'])
        self.assertEqual(Mixin._normalise('  Business\n  JUSTIFICATION '),
                         'business justification')

    def test_read_csv(self):
        expected = [['Tenant', 'User', 'License'], ['contoso.com', 'adele@contoso.com', 'E3']]
        # Comma, semicolon and tab separated, with the byte order mark written by Excel
        for content in ('Tenant,User,License\ncontoso.com,adele@contoso.com,E3\n',
                        '﻿Tenant;User;License\r\ncontoso.com;adele@contoso.com;E3\r\n',
                        'Tenant\tUser\tLicense\ncontoso.com\tadele@contoso.com\tE3\n'):
            self.assertEqual(self._rows(content.encode(), 'FILE.CSV'), expected)
        # Blank rows are skipped, cells are stripped
        content = 'Tenant,User\n , \n\ncontoso.com , adele@contoso.com\n,,\n'
        self.assertEqual(self._rows(content.encode()),
                         [['Tenant', 'User'], ['contoso.com', 'adele@contoso.com']])
        # Files that are not UTF-8 are read as Latin-1
        content = 'Tenant;User\nSociété Générale;adele@contoso.com\n'.encode('latin-1')
        self.assertEqual(self._rows(content)[1], ['Société Générale', 'adele@contoso.com'])

    def test_read_errors(self):
        wizard = self.env['m365.license.bulk.wizard'].create({})
        with self.assertRaisesRegex(UserError, 'Please upload a CSV or Excel'):
            wizard._read_rows()
        # _read_xlsx is a static method: the message is not translated
        with mute_logger('odoo.tools.translate'), \
                self.assertRaisesRegex(UserError, 'The Excel file could not be read'):
            self._rows(b'not an excel file', 'licenses.xlsx')
        with self.assertRaisesRegex(UserError, 'Unsupported file type'):
            self._rows(b'a,b\n', 'licenses.xls')
