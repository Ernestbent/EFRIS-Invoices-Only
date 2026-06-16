import json
import base64
import binascii
import frappe
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

from autozoneura.autozoneura.background_tasks.efris_key_manager import (
    get_private_key,
    resolve_file_path,
    PFX_PASSWORD,
    test_efris_complete_flow
)

CACHE_KEY_AES = "efris_cached_aes_key"

def get_cached_aes_key():
    """Retrieve AES key from cache or regenerate."""
    aes_key_hex = frappe.cache().get_value(CACHE_KEY_AES)
    if not aes_key_hex:
        result = test_efris_complete_flow()
        if result.get("success"):
            aes_key_hex = result.get("aes_key")
        else:
            frappe.throw(f"Failed to regenerate AES key: {result.get('error')}")
    try:
        return binascii.unhexlify(aes_key_hex)
    except Exception as e:
        frappe.throw(f"Failed to convert AES key from hex to bytes: {str(e)}")

def encrypt_and_sign_payload(payload_dict, aes_key_bytes, private_key):
    json_str = json.dumps(payload_dict, separators=(',', ':')).encode('utf-8')
    padded_data = pad(json_str, AES.block_size)

    cipher = AES.new(aes_key_bytes, AES.MODE_ECB)
    ciphertext = cipher.encrypt(padded_data)
    content_b64 = base64.b64encode(ciphertext).decode('utf-8')

    signature = private_key.sign(
        content_b64.encode('utf-8'),
        asym_padding.PKCS1v15(),
        hashes.SHA1()
    )
    signature_b64 = base64.b64encode(signature).decode('utf-8')

    return {
        "content": content_b64,
        "signature": signature_b64
    }

@frappe.whitelist()
def encrypt_dynamic_json(json_input=None):
    try:
        # Get single EFRIS Settings doc
        settings = frappe.get_doc("EFRIS Settings", "EFRIS Settings")
        if not settings.private_key:
            frappe.throw("Private key file not configured in EFRIS Settings")

        private_key = get_private_key(resolve_file_path(settings.private_key), PFX_PASSWORD)
        aes_key = get_cached_aes_key()

        payload = json.loads(json_input) if isinstance(json_input, str) else json_input or {"sample": "data"}

        result = encrypt_and_sign_payload(payload, aes_key, private_key)

        return {
            "success": True,
            "encrypted_content": result["content"],
            "signature": result["signature"]
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }