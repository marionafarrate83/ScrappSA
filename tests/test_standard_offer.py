"""The public offer and payment flow must agree on one lifetime purchase."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

# Import without starting MongoDB index creation; all external writes are mocked.
with patch('threading.Thread.start'):
    import app as application


class StandardOfferTests(unittest.TestCase):
    def setUp(self):
        self.app = application.app
        self.app.config.update(TESTING=True, SECRET_KEY='offer-test-only')
        self.client = self.app.test_client()
        self.stripe_key = patch.object(application.stripe, 'api_key', 'test-only-not-a-real-key')
        self.stripe_key.start()
        self.addCleanup(self.stripe_key.stop)

    def test_only_standard_is_sold(self):
        self.assertEqual(set(application.STRIPE_PLANS), {'estandar'})
        for path in ['/', '/checkout', '/checkout?plan=pro']:
            response = self.client.get(path)
            text = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn('pago único', text.lower())
            self.assertNotIn('de por vida', text.lower())
            self.assertIn('$520 MXN', text)
            self.assertNotIn('$799', text)
            self.assertNotIn('Elegir Pro', text)
            self.assertNotIn('demostración', text.lower())
        self.assertNotIn('type="radio"', self.client.get('/checkout?plan=pro').get_data(as_text=True))

    def test_form_plan_cannot_reactivate_pro(self):
        with patch.object(application.stripe.checkout.Session, 'create', return_value=SimpleNamespace(url='https://checkout.stripe.com/test')) as create:
            response = self.client.post('/checkout', data={'plan':'pro', 'name':'Ana', 'email':'ana@example.com'})
        self.assertEqual(response.status_code, 303)
        options = create.call_args.kwargs
        self.assertEqual(options['mode'], 'payment')
        self.assertEqual(options['metadata']['plan'], 'estandar')
        self.assertEqual(options['metadata']['access_type'], 'lifetime')
        self.assertEqual(options['line_items'][0]['price_data']['unit_amount'], 52000)
        self.assertNotIn('recurring', options['line_items'][0]['price_data'])

    def test_no_payment_configuration_cannot_grant_demo_access(self):
        with patch.object(application.stripe, 'api_key', ''):
            response = self.client.post('/checkout', data={'name':'Ana', 'email':'ana@example.com'})
            self.assertEqual(response.status_code, 503)
            self.assertIn('Pago no disponible', response.get_data(as_text=True))
        with self.client.session_transaction() as session:
            self.assertNotIn('purchase_verified', session)

    def test_invalid_email_keeps_real_payment_context(self):
        with patch.object(application.stripe.checkout.Session, 'create') as create:
            response = self.client.post('/checkout', data={'name':'Ana', 'email':'invalid'})
            create.assert_not_called()
        self.assertEqual(response.status_code,400)
        self.assertIn('correo electrónico válido',response.get_data(as_text=True))
        self.assertIn('Pago único con Stripe',response.get_data(as_text=True))

    def test_payment_error_is_visible_without_internal_details(self):
        with patch.object(application.stripe.checkout.Session, 'create', side_effect=RuntimeError('private-provider-detail')), patch.object(self.app.logger, 'exception'):
            response = self.client.post('/checkout', data={'name':'Ana','email':'ana@example.com'})
        self.assertEqual(response.status_code,502)
        self.assertIn('No se pudo abrir el pago',response.get_data(as_text=True))
        self.assertNotIn('private-provider-detail',response.get_data(as_text=True))

    def test_registration_records_lifetime_access(self):
        with self.client.session_transaction() as session:
            session['purchase_verified']={'verified':True,'plan':'estandar','email':'ana@example.com','stripe_session_id':'test-session'}
        users = Mock()
        users.find_one.return_value = None
        users.insert_one.return_value = SimpleNamespace(inserted_id='new-user')
        purchases = Mock()
        purchases.find_one.return_value = {'status':'paid'}
        with patch.object(application,'users_col',users), patch.object(application,'purchases_col',purchases), patch.object(application,'log_audit'):
            response = self.client.post('/register',data={'email':'ana@example.com','password':'long-password','confirm_password':'long-password'})
        self.assertEqual(response.status_code,302)
        document = users.insert_one.call_args.args[0]
        self.assertEqual(document['plan'],'estandar')
        self.assertEqual(document['access_type'],'lifetime')
        self.assertNotIn('expires_at',document)

    def test_old_demo_session_is_not_a_confirmed_purchase(self):
        with self.client.session_transaction() as session:
            session['purchase_verified']={'verified':True,'demo':True,'email':'ana@example.com'}
        response=self.client.get('/register')
        self.assertEqual(response.status_code,302)
        self.assertTrue(response.location.endswith('/checkout'))

    def test_confirmation_email_matches_offer_and_escapes_customer_data(self):
        with patch.dict(os.environ,{'RESEND_API_KEY':'mock-only'}), patch.object(application.requests,'post') as post:
            self.assertTrue(application.send_purchase_confirmation('ana@example.com','<b>Ana</b>','estandar','https://example.com/register'))
        content=post.call_args.kwargs['json']
        self.assertIn('Estándar',content['subject'])
        self.assertIn('pago único',content['html'])
        self.assertIn('$520.00 MXN',content['html'])
        self.assertIn('&lt;b&gt;Ana&lt;/b&gt;',content['html'])
        self.assertNotIn('<b>Ana</b>',content['html'])


if __name__ == '__main__':
    unittest.main()
