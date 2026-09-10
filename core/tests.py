from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()

SIGNUP_URL = "/auth/users/"
PASSWORD = "Str0ng-Pass-4823"


class SignupCapturesProfileFieldsTests(TestCase):
    """first_name, last_name and phone must survive djoser's create serializer.

    Djoser derives Meta.fields from User.REQUIRED_FIELDS, which is empty here,
    so before core.serializers.UserCreatePasswordRetypeSerializer existed the
    endpoint accepted only email/password -- and ModelSerializer discards
    unknown keys silently, so Register.jsx's first_name and last_name were
    dropped without any error.
    """

    def payload(self, **overrides):
        data = {
            "email": "newuser@example.com",
            "password": PASSWORD,
            "re_password": PASSWORD,
            "first_name": "Ayesha",
            "last_name": "Khan",
            "phone": "03001234567",
        }
        data.update(overrides)
        return data

    def test_all_three_fields_are_persisted(self):
        response = self.client.post(SIGNUP_URL, self.payload())
        self.assertEqual(response.status_code, 201, response.content)

        user = User.objects.get(email="newuser@example.com")
        self.assertEqual(user.first_name, "Ayesha")
        self.assertEqual(user.last_name, "Khan")
        self.assertEqual(user.phone, "03001234567")

    def test_response_echoes_the_new_fields(self):
        response = self.client.post(SIGNUP_URL, self.payload())
        body = response.json()
        self.assertEqual(body["first_name"], "Ayesha")
        self.assertEqual(body["last_name"], "Khan")
        self.assertEqual(body["phone"], "03001234567")
        self.assertNotIn("password", body)

    def test_exact_register_form_payload(self):
        """Register.jsx posts these keys verbatim, including `terms`."""
        response = self.client.post(
            SIGNUP_URL,
            {
                "first_name": "Bilal",
                "last_name": "Ahmed",
                "email": "bilal@example.com",
                "password": PASSWORD,
                "re_password": PASSWORD,
                "terms": True,
            },
        )
        self.assertEqual(response.status_code, 201, response.content)

        user = User.objects.get(email="bilal@example.com")
        self.assertEqual(user.first_name, "Bilal")
        self.assertEqual(user.last_name, "Ahmed")
        self.assertEqual(user.phone, "")  # form has no phone field yet

    def test_fields_stay_optional(self):
        """They are blank=True on the model, so omitting them must still work."""
        response = self.client.post(
            SIGNUP_URL,
            {
                "email": "minimal@example.com",
                "password": PASSWORD,
                "re_password": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 201, response.content)

        user = User.objects.get(email="minimal@example.com")
        self.assertEqual(user.first_name, "")
        self.assertEqual(user.phone, "")

    def test_password_retype_still_enforced(self):
        response = self.client.post(
            SIGNUP_URL, self.payload(re_password="something-else-entirely")
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email="newuser@example.com").exists())

    def test_phone_respects_model_max_length(self):
        response = self.client.post(SIGNUP_URL, self.payload(phone="0" * 26))
        self.assertEqual(response.status_code, 400)
        self.assertIn("phone", response.json())
