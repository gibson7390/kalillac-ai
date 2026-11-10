"""Registration-only cryptography. Imported without loading a cipher or any secret."""
from __future__ import annotations
import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import re
from types import MappingProxyType
import uuid

class RegistrationInvalid(Exception):
    def __init__(self): super().__init__("invalid_or_expired_token")
class RegistrationUnavailable(Exception):
    def __init__(self, retry=False):
        self.retry=retry
        super().__init__("registration_unavailable")
class RegistrationPending(Exception):
    def __init__(self): super().__init__("registration_pending")
class RegistrationConflict(Exception):
    def __init__(self): super().__init__("already_registered_sign_in")

def strict_json(raw):
    def pairs(items):
        result = {}
        for k,v in items:
            if k in result: raise RegistrationUnavailable()
            result[k] = v
        return result
    def bad(_): raise RegistrationUnavailable()
    try:
        def finite(value):
            import math
            number=float(value)
            if not math.isfinite(number):raise RegistrationUnavailable()
            return number
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=bad, parse_float=finite)
    except (ValueError, UnicodeError, RecursionError, AttributeError):
        raise RegistrationUnavailable() from None

def token_valid(token):
    if not isinstance(token,str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}",token): return False
    try: return base64.urlsafe_b64encode(base64.urlsafe_b64decode(token+"=")).decode().rstrip("=")==token
    except ValueError: return False

def new_token():
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")

def token_digest(token):
    if not token_valid(token): raise RegistrationInvalid()
    return hashlib.sha256(b"kalillac-registration-token-v1\0"+token.encode("ascii")).hexdigest()

def _key(value):
    if not isinstance(value,str): raise RegistrationUnavailable()
    try: raw=base64.b64decode(value,validate=True)
    except ValueError: raise RegistrationUnavailable() from None
    if len(raw)!=32 or base64.b64encode(raw).decode("ascii")!=value: raise RegistrationUnavailable()
    return raw

@dataclass(frozen=True,repr=False)
class Keyring:
    active_encryption: str
    active_lookup: str
    encryption_keys: object = field(repr=False)
    lookup_keys: object = field(repr=False)

    @classmethod
    def parse(cls,raw):
        obj=strict_json(raw)
        if not isinstance(obj,dict) or set(obj)!={"format_version","active_encryption","active_lookup","encryption_keys","lookup_keys"} or type(obj["format_version"]) is not int or obj["format_version"]!=1:
            raise RegistrationUnavailable()
        if not isinstance(obj["active_encryption"],str) or not isinstance(obj["active_lookup"],str):raise RegistrationUnavailable()
        groups=[]
        material=set()
        for family in ["encryption","lookup"]:
            items=obj[family+"_keys"]
            if not isinstance(items,list) or not 1<=len(items)<=8:raise RegistrationUnavailable()
            parsed={}
            for entry in items:
                if not isinstance(entry,dict) or set(entry)!={"version","key_b64"}:raise RegistrationUnavailable()
                version=entry["version"]
                prefix="reg-enc" if family=="encryption" else "reg-lookup"
                if not isinstance(version,str) or not re.fullmatch(prefix+r"-v[1-9][0-9]{0,8}",version) or version in parsed:raise RegistrationUnavailable()
                key=_key(entry["key_b64"])
                if key in material:raise RegistrationUnavailable()
                parsed[version]=key;material.add(key)
            if obj["active_"+family] not in parsed:raise RegistrationUnavailable()
            groups.append(MappingProxyType(parsed))
        return cls(obj["active_encryption"],obj["active_lookup"],*groups)

    def pairs(self,email):
        return tuple((version,hmac.new(key,b"kalillac-registration-address-v1\0"+email.encode("utf-8"),hashlib.sha256).hexdigest()) for version,key in self.lookup_keys.items())

    def binding(self,email,version,digest):
        key=self.lookup_keys.get(version)
        return key is not None and hmac.compare_digest(hmac.new(key,b"kalillac-registration-address-v1\0"+email.encode("utf-8"),hashlib.sha256).hexdigest(),digest)

    def decrypt(self,row):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            key=self.encryption_keys[row.email_key_version]
            email=AESGCM(key).decrypt(row.email_nonce,row.email_ciphertext,aad(row)).decode("utf-8")
            if (email!=email.strip().lower() or len(email)>320 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",email)
                or not self.binding(email,row.address_lookup_key_version,row.address_lookup_digest)):
                raise RegistrationUnavailable()
            return email
        except Exception:
            raise RegistrationUnavailable() from None

    def encrypt(self,row,email):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        row.email_key_version=self.active_encryption
        row.email_nonce=os.urandom(12)
        row.email_ciphertext=AESGCM(self.encryption_keys[self.active_encryption]).encrypt(row.email_nonce,email.encode("utf-8"),aad(row))

def aad(row):
    enc=row.email_key_version.encode("ascii");lookup=row.address_lookup_key_version.encode("ascii")
    if row.format_version!=1 or row.purpose!="finish_registration" or not 0<row.generation<2**64:
        raise RegistrationUnavailable()
    return (b"kalillac-registration-intent\0finish_registration\0"+row.format_version.to_bytes(4,"big")
            +row.id.bytes+row.generation.to_bytes(8,"big")+bytes([len(enc)])+enc+bytes([len(lookup)])+lookup
            +bytes.fromhex(row.address_lookup_digest))

def advisory_keys(pairs):
    return tuple(sorted({int.from_bytes(hashlib.sha256(b"kalillac-registration-lock-v1\0"+v.encode("ascii")+b"\0"+bytes.fromhex(d)).digest()[:8],"big",signed=True) for v,d in pairs}))
