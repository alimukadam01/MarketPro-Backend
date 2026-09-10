from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import WALK_IN_CUSTOMER_NAME, Business, City, Customer

User = get_user_model()


class WalkInCustomerSignalTests(TestCase):
    """Every new business gets the counter-sale customer.

    Existing businesses deliberately do not get one backfilled — their invoices
    simply take the plain-download path.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="walkin-signal@example.com", password="pw-for-tests"
        )

    def make_business(self, name="Canary Traders"):
        return Business.objects.create(
            name=name, owner=self.user, phone="03001234567"
        )

    def walk_ins_for(self, business):
        return Customer.objects.filter(
            business=business, name=WALK_IN_CUSTOMER_NAME
        )

    def test_new_business_gets_exactly_one_walk_in(self):
        City.objects.create(name="Karachi", postal_code="75000")
        business = self.make_business()

        self.assertEqual(self.walk_ins_for(business).count(), 1)

    def test_walk_in_uses_the_lowest_city_id(self):
        first = City.objects.create(name="Karachi", postal_code="75000")
        City.objects.create(name="Hyderabad", postal_code="71000")

        business = self.make_business()

        self.assertEqual(self.walk_ins_for(business).first().city_id, first.id)

    def test_business_is_still_created_with_no_cities(self):
        """Customer.city is NOT NULL, so with no City rows there is nothing to
        attach. That must not take business creation down with it."""
        self.assertFalse(City.objects.exists())

        business = self.make_business()

        self.assertIsNotNone(business.pk)
        self.assertEqual(self.walk_ins_for(business).count(), 0)

    def test_each_business_gets_its_own_scoped_to_it(self):
        City.objects.create(name="Karachi", postal_code="75000")

        one = self.make_business("Business One")
        two = self.make_business("Business Two")

        self.assertEqual(self.walk_ins_for(one).count(), 1)
        self.assertEqual(self.walk_ins_for(two).count(), 1)
        self.assertNotEqual(
            self.walk_ins_for(one).first().pk, self.walk_ins_for(two).first().pk
        )

    def test_resaving_a_business_does_not_create_a_second(self):
        City.objects.create(name="Karachi", postal_code="75000")
        business = self.make_business()

        business.address = "Saddar"
        business.save()

        self.assertEqual(self.walk_ins_for(business).count(), 1)

    def test_manager_is_idempotent(self):
        """create_walk_in is get_or_create, so calling it again adopts the row."""
        City.objects.create(name="Karachi", postal_code="75000")
        business = self.make_business()

        again = Customer.objects.create_walk_in(business.id)

        self.assertEqual(self.walk_ins_for(business).count(), 1)
        self.assertEqual(again.pk, self.walk_ins_for(business).first().pk)
