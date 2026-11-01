"""Canonical JSON artifacts; no archives, executable files or implicit trust."""
import base64
import hashlib
import json
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

MAX_ARTIFACT = 256 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def public_key(private):
    return base64.b64encode(private.public_key().public_bytes_raw()).decode()


def key_id(public):
    return sha(base64.b64decode(public, validate=True))[:32]


def sign(private, value):
    return base64.b64encode(private.sign(canonical(value))).decode()


def verify(public, value, signature):
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public, validate=True)).verify(
        base64.b64decode(signature, validate=True), canonical(value))


def load_private(path):
    from pathlib import Path
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(Path(path).read_text().strip(), validate=True))


def signed(private, payload):
    return {'key_id': key_id(public_key(private)), 'payload': payload, 'signature': sign(private, payload)}
