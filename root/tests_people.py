"""
People, Phase 2: Customer and Supplier are roles of a Person.

The details live on the person, the /customers/ and /suppliers/ API reads
and writes them under the same names as before, and a role is never deleted
on its own: only a person is (register R13, S1).
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from .models import (
    WALK_IN_CUSTOMER_NAME, Business, City, Customer, Person, Supplier,
)

User = get_user_model()


def client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'JWT {AccessToken.for_user(user)}')
    return client


class PeopleFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        # The city first: the walk-in customer a new business gets needs one.
        cls.city = City.objects.create(name='Hyderabad', postal_code='71000')
        cls.owner = User.objects.create_user(email='people-owner@example.com', password='pw-for-tests')
        cls.business = Business.objects.create(name='People Paints', owner=cls.owner,
                                               phone='03001234567', is_active=True)

    def setUp(self):
        self.client = client_for(self.owner)


class RoleRecordTests(PeopleFixture):

    def test_creating_a_role_makes_its_person_and_stores_the_details_once(self):
        customer = Customer.objects.create(business=self.business, name='Ali Traders',
                                           phone='0300-1234567', city=self.city)
        person = customer.person
        self.assertEqual((person.name, person.phone, person.city_id, person.business_id),
                         ('Ali Traders', '0300-1234567', self.city.id, self.business.id))
        # Read through the role, stored only on the person.
        self.assertEqual(customer.name, 'Ali Traders')
        self.assertNotIn('name', [f.name for f in Customer._meta.concrete_fields])
        self.assertTrue(customer.is_active)

    def test_one_person_can_hold_both_roles(self):
        customer = Customer.objects.create(business=self.business, name='Ali Traders', city=self.city)
        supplier = Supplier.objects.create(business=self.business, person=customer.person)
        self.assertEqual(supplier.person_id, customer.person_id)
        self.assertEqual(supplier.name, 'Ali Traders')
        self.assertEqual(Person.objects.filter(business=self.business, name='Ali Traders').count(), 1)

    def test_a_supplier_gets_a_creation_date(self):
        supplier = Supplier.objects.create(business=self.business, name='Jotun')
        self.assertIsNotNone(supplier.created_at)

    def test_every_business_still_gets_one_walk_in_customer(self):
        walk_ins = Customer.objects.filter(business=self.business, person__name=WALK_IN_CUSTOMER_NAME)
        self.assertEqual(walk_ins.count(), 1)
        self.assertEqual(Customer.objects.create_walk_in(self.business.id), walk_ins.get())

    def test_deleting_the_person_removes_its_roles(self):
        customer = Customer.objects.create(business=self.business, name='Gone', city=self.city)
        supplier = Supplier.objects.create(business=self.business, person=customer.person)
        customer.person.delete()
        self.assertFalse(Customer.objects.filter(pk=customer.pk).exists())
        self.assertFalse(Supplier.objects.filter(pk=supplier.pk).exists())


class RoleAPITests(PeopleFixture):

    def test_the_customer_api_keeps_its_fields_and_writes_the_person(self):
        created = self.client.post('/customers/', {
            'name': 'Hamza Furniture', 'phone': '0333-4410087', 'city': self.city.id,
            'address': 'Latifabad', 'email': 'hamza@example.com', 'notes': 'Pays on the 1st',
        }, format='json')
        self.assertEqual(created.status_code, 201, created.content)
        customer = Customer.objects.get(pk=created.json()['id'])
        self.assertEqual(customer.person.address, 'Latifabad')

        updated = self.client.patch(f'/customers/{customer.id}/', {'phone': '0333-0000000'}, format='json')
        self.assertEqual(updated.status_code, 200, updated.content)
        customer.person.refresh_from_db()
        self.assertEqual(customer.person.phone, '0333-0000000')

        detail = self.client.get(f'/customers/{customer.id}/').json()
        for key in ('id', 'name', 'phone', 'email', 'address', 'city', 'notes', 'created_at'):
            self.assertIn(key, detail)
        self.assertEqual(detail['name'], 'Hamza Furniture')

    def test_a_customer_still_needs_a_name_and_a_city(self):
        response = self.client.post('/customers/', {'phone': '1'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('name', response.json())
        self.assertIn('city', response.json())

    def test_the_supplier_api_keeps_its_fields_and_writes_the_person(self):
        created = self.client.post('/suppliers/', {
            'name': 'Bashir', 'business_name': 'Bashir Hardware', 'phone': '0321-5550192',
        }, format='json')
        self.assertEqual(created.status_code, 201, created.content)
        supplier = Supplier.objects.get(pk=created.json()['id'])
        self.assertEqual(supplier.person.business_name, 'Bashir Hardware')
        listed = self.client.get('/suppliers/', {'search': 'Hardware'}).json()
        rows = listed['results'] if isinstance(listed, dict) else listed
        self.assertEqual([row['id'] for row in rows], [supplier.id])

    def test_searching_customers_reads_the_person(self):
        Customer.objects.create(business=self.business, name='Zubair Khan', phone='0301-9902215', city=self.city)
        listed = self.client.get('/customers/', {'search': 'Zubair'}).json()
        rows = listed['results'] if isinstance(listed, dict) else listed
        self.assertEqual([row['name'] for row in rows], ['Zubair Khan'])


class RolesAreNotDeletedTests(PeopleFixture):
    """A customer or supplier is deactivated, never deleted; only a person is."""

    def test_a_customer_cannot_be_deleted(self):
        customer = Customer.objects.create(business=self.business, name='Kept', city=self.city)
        self.assertEqual(self.client.delete(f'/customers/{customer.id}/').status_code, 405)
        bulk = self.client.post('/customers/bulk-delete/', {'customer_ids': [customer.id]}, format='json')
        self.assertEqual(bulk.status_code, 405)
        self.assertIn('Deactivate', bulk.json()['detail'])
        self.assertTrue(Customer.objects.filter(pk=customer.pk).exists())

    def test_a_supplier_cannot_be_deleted(self):
        supplier = Supplier.objects.create(business=self.business, name='Kept')
        self.assertEqual(self.client.delete(f'/suppliers/{supplier.id}/').status_code, 405)
        bulk = self.client.post('/suppliers/bulk-delete/', {'supplier_ids': [supplier.id]}, format='json')
        self.assertEqual(bulk.status_code, 405)
        self.assertTrue(Supplier.objects.filter(pk=supplier.pk).exists())
