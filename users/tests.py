from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Card, Exchange, MeetingAgreement, TradeOffer, UserCard


class CoreFlowSmokeTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.seller = User.objects.create_user(username='seller', password='pass12345')
        self.buyer = User.objects.create_user(username='buyer', password='pass12345')
        self.card = Card.objects.create(
            name='Young Pyromancer',
            set_name='Foundations Jumpstart',
            set_code='J25',
            collector_number='618',
            price=Decimal('20.00'),
        )
        self.listing = UserCard.objects.create(
            user=self.seller,
            card=self.card,
            is_owned=True,
            quantity_owned=2,
            listing_intent='sell_trade',
            asking_price=Decimal('20.00'),
        )

    def test_public_marketplace_pages_render(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse('marketplace'))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse('marketplace_card_detail', args=[self.card.id]))
        self.assertEqual(response.status_code, 200)

    def test_collection_requires_login(self):
        response = self.client.get(reverse('card_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_purchase_offer_range_is_enforced(self):
        self.client.force_login(self.buyer)
        url = reverse('make_purchase_offer')
        base_payload = {
            'card_name': self.card.name,
            'owner_id': self.seller.id,
            'listing_id': self.listing.id,
            'purchase_mode': 'offer',
            'next': reverse('sale_listing_detail', args=[self.listing.id]),
        }

        response = self.client.post(url, {**base_payload, 'offer_amount': '10.00'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Exchange.objects.exists())

        response = self.client.post(url, {**base_payload, 'offer_amount': '15.00'})
        self.assertEqual(response.status_code, 302)
        exchange = Exchange.objects.get()
        self.assertEqual(exchange.offer_amount, Decimal('15.00'))
        self.assertEqual(exchange.status, 'pending')

    def test_offer_state_changes_require_post(self):
        sale_exchange = Exchange.objects.create(
            sender=self.buyer,
            receiver=self.seller,
            listing=self.listing,
            target_card=self.card,
            sender_cards=f'listing:{self.listing.id}',
            receiver_cards=self.card.name,
            exchange_type='sale',
            status='pending',
            offer_amount=Decimal('20.00'),
            agreed_price=Decimal('20.00'),
        )
        trade_offer = TradeOffer.objects.create(
            sender=self.buyer,
            receiver=self.seller,
            requested_listing=self.listing,
            requested_listing_price=Decimal('20.00'),
            offered_cards_value=Decimal('0.00'),
            cash_due=Decimal('20.00'),
            status='pending',
        )

        self.client.force_login(self.seller)
        endpoints = [
            reverse('accept_exchange', args=[sale_exchange.id]),
            reverse('reject_exchange', args=[sale_exchange.id]),
            reverse('accept_trade_offer', args=[trade_offer.id]),
            reverse('reject_trade_offer', args=[trade_offer.id]),
        ]
        for endpoint in endpoints:
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 405)

        sale_exchange.refresh_from_db()
        trade_offer.refresh_from_db()
        self.assertEqual(sale_exchange.status, 'pending')
        self.assertEqual(trade_offer.status, 'pending')

    def test_completed_sale_decrements_inventory_once(self):
        exchange = Exchange.objects.create(
            sender=self.buyer,
            receiver=self.seller,
            listing=self.listing,
            target_card=self.card,
            sender_cards=f'listing:{self.listing.id}',
            receiver_cards=self.card.name,
            exchange_type='sale',
            status='accepted',
            offer_amount=Decimal('20.00'),
            agreed_price=Decimal('20.00'),
        )
        MeetingAgreement.objects.create(exchange=exchange)

        self.client.force_login(self.buyer)
        url = reverse('meeting_exchange', args=[exchange.id])
        response = self.client.post(url, {'action': 'complete', 'rating': '5'})
        self.assertEqual(response.status_code, 302)
        self.listing.refresh_from_db()
        exchange.refresh_from_db()
        self.assertEqual(self.listing.quantity_owned, 1)
        self.assertTrue(exchange.inventory_settled)
        self.assertEqual(exchange.status, 'completed')

        response = self.client.post(url, {'action': 'complete', 'rating': '5'})
        self.assertEqual(response.status_code, 302)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.quantity_owned, 1)
