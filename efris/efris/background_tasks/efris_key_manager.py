import base64
import json
import os
import requests
from datetime import datetime
import frappe
from frappe import cache
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
import binascii

CACHE_KEY_AES = "efris_cached_aes_key"

def resolve_file_path(file_url):
    """Resolve file path from Frappe file URL"""
    if not file_url:
        frappe.throw("No file URL provided")
    
    file_name = file_url.split("/")[-1]
    
    # Try private files first
    file_path = os.path.join(frappe.get_site_path("private", "files"), file_name)
    if os.path.exists(file_path):
        return file_path
    
    # Try public files
    file_path = os.path.join(frappe.get_site_path("public", "files"), file_name)
    if os.path.exists(file_path):
        return file_path
    
    frappe.throw(f"Private key file not found: {file_name}")

def get_private_key(pfx_path, password):
    """Load private key from PFX file"""
    with open(pfx_path, 'rb') as f:
        pfx_data = f.read()
    
    password_bytes = password if isinstance(password, bytes) else password.encode('utf-8')
    
    try:
        pfx = pkcs12.load_key_and_certificates(pfx_data, password_bytes, default_backend())
        private_key = pfx[0]
        
        if not private_key:
            raise Exception("No private key in PFX")
        
        return private_key
    except Exception as e:
        # Try with empty password
        try:
            pfx = pkcs12.load_key_and_certificates(pfx_data, b"", default_backend())
            private_key = pfx[0]
            if private_key:
                return private_key
        except:
            pass
        raise Exception(f"Failed to load private key: {str(e)}")


def get_pfx_password(settings):
    try:
        password = settings.get_password("password")
    except Exception:
        password = None

    if not password:
        password = getattr(settings, "password", None)

    return password or ""

def decrypt_passwordDes(passwordDes_b64, private_key):
    """
    Decrypt passwordDes to get AES key
    Per EFRIS spec: passwordDes is encrypted with client's public key
    """
    # Base64 decode
    encrypted_data = base64.b64decode(passwordDes_b64)
    
    print(f"Attempting decryption...")
    print(f"  - Encrypted data size: {len(encrypted_data)} bytes")
    print(f"  - Private key size: {private_key.key_size} bits")
    
    # Method 1: PKCS1v15 with cryptography library
    try:
        print("  - Trying cryptography PKCS1v15...")
        decrypted = private_key.decrypt(encrypted_data, padding.PKCS1v15())
        print(f"    SUCCESS! Decrypted length: {len(decrypted)} bytes")
        print(f"    Decrypted (first 50 chars): {decrypted[:50]}")
        
        # Try to decode as base64
        try:
            aes_key_raw = base64.b64decode(decrypted)
            print(f"    Base64 decoded to {len(aes_key_raw)} bytes")
            
            if len(aes_key_raw) in [16, 24, 32]:
                aes_key_hex = binascii.hexlify(aes_key_raw).decode('utf-8')
                print(f"    Valid AES key: {len(aes_key_raw)} bytes")
                return aes_key_hex
        except:
            # Maybe it's already raw bytes
            if len(decrypted) in [16, 24, 32]:
                aes_key_hex = binascii.hexlify(decrypted).decode('utf-8')
                print(f"    Using raw decrypted as AES key: {len(decrypted)} bytes")
                return aes_key_hex
        
        # If we got here, invalid length
        raise ValueError(f"Invalid AES key length after decryption: {len(decrypted)} bytes")
        
    except Exception as e1:
        print(f"    Failed: {str(e1)[:100]}")
    
    # Method 2: Try PyCryptodome PKCS1_v1_5
    try:
        print("  - Trying PyCryptodome PKCS1_v1_5...")
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
        from Crypto.Cipher import PKCS1_v1_5
        from Crypto.PublicKey import RSA
        
        # Convert private key to PEM format
        pkey_pem = private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption()
        )
        
        rsa_key = RSA.import_key(pkey_pem)
        cipher = PKCS1_v1_5.new(rsa_key)
        decrypted = cipher.decrypt(encrypted_data, None)
        
        if decrypted:
            print(f"    SUCCESS! Decrypted length: {len(decrypted)} bytes")
            print(f"    Decrypted (first 50 chars): {decrypted[:50]}")
            
            # Try to decode as base64
            try:
                aes_key_raw = base64.b64decode(decrypted)
                print(f"    Base64 decoded to {len(aes_key_raw)} bytes")
                
                if len(aes_key_raw) in [16, 24, 32]:
                    aes_key_hex = binascii.hexlify(aes_key_raw).decode('utf-8')
                    print(f"    Valid AES key: {len(aes_key_raw)} bytes")
                    return aes_key_hex
            except:
                if len(decrypted) in [16, 24, 32]:
                    aes_key_hex = binascii.hexlify(decrypted).decode('utf-8')
                    print(f"    Using raw decrypted as AES key: {len(decrypted)} bytes")
                    return aes_key_hex
            
            raise ValueError(f"Invalid AES key length: {len(decrypted)} bytes")
        else:
            print(f"    Failed: returned None")
            
    except Exception as e2:
        print(f"    Failed: {str(e2)[:100]}")
    
    # Method 3: OAEP with SHA1 (some systems use this)
    try:
        print("  - Trying OAEP SHA1...")
        decrypted = private_key.decrypt(
            encrypted_data,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA1()),
                algorithm=hashes.SHA1(),
                label=None
            )
        )
        print(f"    SUCCESS! Decrypted length: {len(decrypted)} bytes")
        
        try:
            aes_key_raw = base64.b64decode(decrypted)
            if len(aes_key_raw) in [16, 24, 32]:
                aes_key_hex = binascii.hexlify(aes_key_raw).decode('utf-8')
                return aes_key_hex
        except:
            if len(decrypted) in [16, 24, 32]:
                aes_key_hex = binascii.hexlify(decrypted).decode('utf-8')
                return aes_key_hex
                
    except Exception as e3:
        print(f"    Failed: {str(e3)[:100]}")
    
    raise Exception("All decryption methods failed. Check if correct private key is uploaded to EFRIS portal.")

def make_t104_request(server_url, device_number, tin, brn=""):
    """
    Make T104 API call to get new AES key
    Per EFRIS spec: AES key valid for 24 hours
    """
    payload = {
        "data": {
            "content": "",
            "signature": "",
            "dataDescription": {
                "codeType": "0",
                "encryptCode": "1",
                "zipCode": "0"
            }
        },
        "globalInfo": {
            "appId": "AP04",
            "version": "1.1.20191201",
            "dataExchangeId": "9230489223014123",
            "interfaceCode": "T104",
            "requestCode": "TP",
            "requestTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "responseCode": "TA",
            "userName": "admin",
            "deviceMAC": "B47720524158",
            "deviceNo": device_number,
            "tin": tin,
            "brn": (brn or "").strip().lstrip("/"),
            "taxpayerID": "1",
            "longitude": "32.61665",
            "latitude": "0.36601",
            "agentType": "0",
            "extendField": {
                "responseDateFormat": "dd/MM/yyyy",
                "responseTimeFormat": "dd/MM/yyyy HH:mm:ss",
                "referenceNo": "24PL01000221",
                "operatorName": "administrator"
            }
        },
        "returnStateInfo": {
            "returnCode": "",
            "returnMessage": ""
        }
    }
    
    response = requests.post(
        server_url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30
    )
    
    if response.status_code != 200:
        raise Exception(f"T104 request failed: HTTP {response.status_code}")
    
    response_data = response.json()
    
    # Check return code
    return_code = response_data.get("returnStateInfo", {}).get("returnCode", "")
    if return_code and return_code != "00":
        return_msg = response_data.get("returnStateInfo", {}).get("returnMessage", "")
        raise Exception(f"T104 error {return_code}: {return_msg}")
    
    # Get and decode content
    content = response_data.get("data", {}).get("content")
    if not content:
        raise Exception("No content in T104 response")
    
    decoded_content = json.loads(base64.b64decode(content).decode('utf-8'))
    return decoded_content

def refresh_efris_aes_key():
    settings = frappe.get_single("EFRIS Settings")

    if not getattr(settings, "active", 0):
        return {"success": False, "error": "EFRIS Settings is disabled"}

    required_fields = {
        "server_url": "Server URL",
        "device_number": "Device Number",
        "tin": "TIN",
        "private_key": "Private Key",
    }

    for fieldname, label in required_fields.items():
        if not getattr(settings, fieldname, None):
            return {"success": False, "error": f"{label} is required in EFRIS Settings"}

    file_path = resolve_file_path(settings.private_key)
    private_key = get_private_key(file_path, get_pfx_password(settings))

    t104_response = make_t104_request(
        settings.server_url,
        settings.device_number,
        settings.tin,
        settings.brn,
    )

    password_des = t104_response.get("passowrdDes")
    if not password_des:
        return {"success": False, "error": "Missing passowrdDes in T104 response"}

    aes_key_hex = decrypt_passwordDes(password_des, private_key)
    cache().set_value(CACHE_KEY_AES, aes_key_hex, expires_in_sec=86400)
    settings.db_set("aes_key", aes_key_hex, update_modified=False)
    frappe.db.commit()

    return {
        "success": True,
        "aes_key": aes_key_hex,
        "aes_key_length": len(aes_key_hex) // 2,
        "cached": True,
    }


@frappe.whitelist()
def test_efris_complete_flow():
    try:
        return refresh_efris_aes_key()
    except Exception as e:
        error_msg = str(e)[:200]
        frappe.log_error(error_msg, "EFRIS Key Manager")
        return {"success": False, "error": str(e)}


def refresh_daily_efris_aes_key():
    try:
        result = refresh_efris_aes_key()
        if not result.get("success"):
            frappe.log_error(result.get("error"), "EFRIS Daily AES Key Refresh")
        return result
    except Exception:
        frappe.log_error(frappe.get_traceback(), "EFRIS Daily AES Key Refresh")
        return {"success": False, "error": "AES key refresh failed"}
