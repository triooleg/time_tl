"""Sign exact manifest bytes using an existing external Ed25519 PEM; never generate keys."""
import argparse
import base64
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from time_tl.runtime.production_release import (
    PRODUCTION_RELEASE_PUBLIC_KEY_HEX,
    load_verified_release_attestation,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--signature', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.key.resolve().is_relative_to(root):
        raise ValueError('Private key must remain outside this repository')
    if args.signature.exists():
        raise ValueError('Refusing to overwrite an existing signature')
    raw = args.manifest.read_bytes()
    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    ).hex()
    if public != PRODUCTION_RELEASE_PUBLIC_KEY_HEX:
        raise ValueError('Existing key does not match compiled release trust anchor')
    encoded = base64.b64encode(key.sign(raw)) + b'\n'
    manifest, fingerprint = load_verified_release_attestation(
        read_manifest_bytes=lambda: raw, read_signature_bytes=lambda: encoded,
    )
    with args.signature.open('xb') as output:
        output.write(encoded)
    print(
        f'PASS signature verified revision={manifest.config_revision} '
        f'semantic_fingerprint={fingerprint}'
    )
    print(f'manifest_raw_sha256={hashlib.sha256(raw).hexdigest()}')
    print(f'signature_sha256={hashlib.sha256(encoded).hexdigest()}')


if __name__ == '__main__':
    main()
