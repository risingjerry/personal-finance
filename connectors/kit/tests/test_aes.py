import unittest

from connectors.kit.aes import cbc_decrypt, cbc_encrypt, decrypt_block, encrypt_block


class AesTests(unittest.TestCase):
    def test_fips197_vector(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
        expected = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(encrypt_block(key, plaintext), expected)
        self.assertEqual(decrypt_block(key, expected), plaintext)

    def test_cbc_round_trip_and_padding(self):
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plaintext = b"hello pfin"
        ciphertext = cbc_encrypt(key, iv, plaintext)
        self.assertNotEqual(ciphertext[: len(plaintext)], plaintext)
        self.assertEqual(cbc_decrypt(key, iv, ciphertext), plaintext)

    def test_cbc_rejects_bad_padding(self):
        key = b"0" * 16
        iv = b" " * 16
        with self.assertRaises(ValueError):
            cbc_decrypt(key, iv, b"not-a-block")


if __name__ == "__main__":
    unittest.main()
