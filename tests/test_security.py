"""Tests for the phone/OTP security primitives (stdlib only; no jose needed)."""

import unittest

from app.core import security


class PhoneTests(unittest.TestCase):
    def test_normalise_accepts_common_forms(self):
        for raw in ["08031234567", "8031234567", "2348031234567", "+234 803 123 4567"]:
            self.assertEqual(security.normalise_phone(raw), "+2348031234567")

    def test_invalid_phone_raises(self):
        with self.assertRaises(ValueError):
            security.normalise_phone("12345")

    def test_hash_is_stable_and_form_independent(self):
        a = security.hash_phone("08031234567")
        b = security.hash_phone("+2348031234567")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)  # sha256 hex

    def test_last4(self):
        self.assertEqual(security.phone_last4("08031234567"), "4567")


class OTPTests(unittest.TestCase):
    def test_otp_length_and_digits(self):
        code = security.generate_otp()
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_otp_hash_verifies(self):
        ph = security.hash_phone("08031234567")
        code = "123456"
        h = security.hash_otp(ph, code)
        self.assertTrue(security.verify_otp(ph, code, h))
        self.assertFalse(security.verify_otp(ph, "000000", h))


if __name__ == "__main__":
    unittest.main()
