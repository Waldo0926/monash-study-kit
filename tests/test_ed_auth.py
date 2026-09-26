import unittest

from monash_study_kit import edlib


class TokenKindTests(unittest.TestCase):
    """设置页的 API 令牌走 Bearer，浏览器里的 JWT 走 x-token；发错头 Ed 直接 401。"""

    JWT = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3MDAwMDAwMDB9.c2ln"
    API = "abcDEF.0123456789abcdefghijklmnopqrstuvwxyzABCD"

    def test_api_token_uses_bearer(self):
        self.assertTrue(edlib.is_api_token(self.API))
        self.assertEqual(edlib.auth_headers(self.API), {"authorization": f"Bearer {self.API}"})

    def test_jwt_uses_x_token(self):
        self.assertFalse(edlib.is_api_token(self.JWT))
        self.assertEqual(edlib.auth_headers(self.JWT), {"x-token": self.JWT})


if __name__ == "__main__":
    unittest.main()
