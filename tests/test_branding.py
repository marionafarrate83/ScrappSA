"""Identidad de marca, versiones de campaña y conservación de UTM."""
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace

# Import without starting MongoDB index creation.
with patch('threading.Thread.start'):
    import app as application
from landing_offers import DEFAULT_OFFER, OFFER_VARIANTS

ROOT = Path(__file__).resolve().parent.parent


class BrandingTests(unittest.TestCase):
    def setUp(self):
        application.app.config.update(TESTING=True, SECRET_KEY='brand-test-only')
        self.client = application.app.test_client()

    def page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_default_landing_message(self):
        text = self.page('/')
        for expected in ('Tus próximos clientes', 'están aquí.', 'Encuentra. Prospecta. Vende.',
                         'Más de 2 millones de empresas', 'Sin mensualidades', 'Empieza a prospectar',
                         'Conoce cómo funciona', 'datos ficticios'):
            self.assertIn(expected, text)
        self.assertNotIn('Testimonios', text)

    def test_variants_change_message_but_not_price(self):
        for key, variant in OFFER_VARIANTS.items():
            with self.subTest(key=key):
                text = self.page(f'/?v={key}')
                self.assertIn(variant['headline'], text)
                self.assertIn(variant['highlight'], text)
                self.assertIn('$520 MXN', text)
                self.assertEqual(text.count('class="card is-featured"'), 1)
                self.assertLessEqual(set(variant), set(DEFAULT_OFFER))

    def test_unknown_variant_falls_back_without_echoing_input(self):
        text = self.page('/?v=<script>alert(1)</script>')
        self.assertIn(DEFAULT_OFFER['headline'], text)
        self.assertNotIn('<script>alert(1)', text)
        self.assertNotIn('is-featured', text)

    def test_utm_parameters_follow_the_visitor_to_checkout(self):
        query = '?v=agencias&utm_source=meta&utm_campaign=a%20b&other=drop'
        text = self.page('/' + query)
        self.assertIn('href="/checkout?utm_source=meta&amp;utm_campaign=a+b"', text)
        self.assertNotIn('href="/checkout"', text)
        self.assertNotIn('other=drop', text)
        checkout = self.page('/checkout?utm_source=meta')
        self.assertIn('action="/checkout?utm_source=meta"', checkout)

    def test_utm_values_are_escaped(self):
        text = self.page('/?utm_source=%22%3E%3Cscript%3E')
        self.assertNotIn('"><script>', text)

    def test_utm_does_not_change_what_stripe_charges(self):
        with patch.object(application.stripe, 'api_key', 'test-only'), \
                patch.object(application.stripe.checkout.Session, 'create',
                             return_value=SimpleNamespace(url='https://checkout.stripe.com/test')) as create:
            response = self.client.post('/checkout?utm_source=meta', data={'name': 'Ana', 'email': 'ana@example.com'})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(create.call_args.kwargs['line_items'][0]['price_data']['unit_amount'], 52000)
        self.assertNotIn('utm', str(create.call_args.kwargs))

    def test_brand_assets_are_served(self):
        for name in ('tokens.css', 'base.css', 'landing.css', 'auth.css', 'app.css', 'admin.css', 'site.webmanifest',
                     'img/logo.svg', 'img/logo-mono.svg', 'img/isotype.svg', 'img/favicon.svg', 'img/favicon.ico',
                     'img/apple-touch-icon.png', 'img/icon-192.png', 'img/icon-512.png',
                     'fonts/inter-latin-wght-normal.woff2', 'fonts/inter-latin-ext-wght-normal.woff2'):
            with self.subTest(name=name):
                response = self.client.get(f'/static/brand/{name}')
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_colors_live_only_in_the_design_system(self):
        files = [*ROOT.glob('templates/**/*.html'), *ROOT.glob('static/**/*.css'), *ROOT.glob('static/*.js')]
        allowed = {ROOT / 'static/brand/tokens.css', ROOT / 'templates/partials/brand_head.html'}
        pattern = r'(?<![&\w])#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![\w-])'
        for path in files:
            if path not in allowed:
                with self.subTest(path=path.name):
                    self.assertNotRegex(path.read_text(encoding='utf-8'), pattern)


if __name__ == '__main__':
    unittest.main()
