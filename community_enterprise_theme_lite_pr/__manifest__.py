# -*- coding: utf-8 -*-
{
    'name': 'Odoo 19 Community to Enterprise Theme FREE | Home Menu & Light UI',
    'version': '19.0.1.0.0',
    'category': 'Themes/Backend',
    'summary': 'FREE — Get Enterprise-style home menu & clean light UI in Odoo 19 Community. No Enterprise license needed.',
    'description': """
        Odoo 19 Community to Enterprise Theme — FREE Edition
        ======================================================
        Give your Odoo 19 Community the Enterprise home menu experience — completely free.

        WHAT YOU GET (FREE)
        -------------------
        * Enterprise-Style Home Menu : Full-screen /odoo app grid — just like Odoo Enterprise
        * Clean Light Theme          : Enterprise-quality light UI with improved navbar
        * App Icon Grid              : All your installed apps displayed beautifully
        * Smooth Animations          : Hover effects and fade-in transitions
        * Mobile Responsive          : Works perfectly on tablets and mobile devices
        * Zero Configuration         : Install and it works immediately

        WHY IS THE HOME MENU SPECIAL?
        ------------------------------
        Odoo Community Edition does not have the /odoo home dashboard page.
        You land directly in the last-used app with no overview.
        This module adds the beautiful full-screen app grid — the #1 most-loved
        feature of Odoo Enterprise — completely free.

        WANT MORE? UPGRADE TO PRO
        --------------------------
        The PRO version ($19) adds:
        - Fuzzy Search: Type to find any app or menu instantly
        - 5 Dark Color Palettes: Amethyst, Midnight Blue, Emerald Forest, Royal Red, Oceanic Blue
        - Glassmorphism Navbar: Frosted-glass effect
        - Per-User Theme Preferences
        - System Auto Dark/Light Mode
        - Accessibility Fixes for all Odoo modules

        Search "Community to Enterprise Theme PRO" on the Odoo App Store.

        KEYWORDS
        --------
        odoo 19 free enterprise theme, odoo community home menu free, odoo 19 light theme,
        enterprise home menu odoo community free, odoo community enterprise look free,
        odoo app grid home menu, odoo 19 community ui improvement, free odoo theme,
        enterprise features community edition free, odoo home dashboard community
    """,
    'author': 'Beautiful Themes PR',
    'support': 'prudhvi.inumarthi99bkp@gmail.com',
    'website': 'https://www.beautiful-odoo-themes.com',
    'license': 'LGPL-3',
    'price': 0.00,
    'currency': 'USD',
    'images': [
        'static/description/banner.png',
        'static/description/theme_screenshot.png',
        'static/description/screenshot_home.png',
    ],
    'depends': ['web'],
    'data': [],
    'assets': {
        'web.assets_backend': [
            'community_enterprise_theme_lite_pr/static/src/home_menu/home_menu.scss',
            'community_enterprise_theme_lite_pr/static/src/webclient/navbar/navbar.scss',
            'community_enterprise_theme_lite_pr/static/src/**/*.js',
            'community_enterprise_theme_lite_pr/static/src/**/*.xml',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
}
