import base64
import binascii
import frappe
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

CACHE_KEY_AES = "efris_cached_aes_key"

def get_cached_aes_key():
    """Get the AES key from cache only; no regeneration."""
    aes_key_hex = frappe.cache().get_value(CACHE_KEY_AES)
    
    if not aes_key_hex:
        frappe.throw("AES key not found in cache. Please ensure it is set before decryption.")
    
    try:
        aes_key_bytes = binascii.unhexlify(aes_key_hex)
        frappe.logger().info(f"✓ AES key loaded from cache, length: {len(aes_key_bytes)} bytes")
        return aes_key_bytes
    except Exception as e:
        frappe.throw(f"Failed to convert AES key from hex to bytes: {str(e)}")

def decrypt_string(encrypted_content_b64):
    """
    Decrypt a base64 encoded encrypted string using cached AES key.
    """
    try:
        aes_key_bytes = get_cached_aes_key()
        ciphertext = base64.b64decode(encrypted_content_b64)
        cipher = AES.new(aes_key_bytes, AES.MODE_ECB)
        padded_plaintext = cipher.decrypt(ciphertext)
        plaintext_bytes = unpad(padded_plaintext, AES.block_size)
        plaintext = plaintext_bytes.decode('utf-8')
        frappe.logger().info("✓ Decryption successful")
        return plaintext
    except Exception as e:
        frappe.logger().error(f"Decryption failed: {str(e)}")
        frappe.throw(f"Failed to decrypt content: {str(e)}")

@frappe.whitelist()
def decrypt_content(encrypted_content=None):
    """Frappe whitelist function to decrypt content."""
    try:
        if not encrypted_content:
            frappe.throw("encrypted_content is required")
        
        decrypted = decrypt_string(encrypted_content)
        return {
            "success": True, 
            "decrypted_content": decrypted
        }
    except Exception as e:
        frappe.log_error(f"decrypt_content error: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }