"""
TREZOR support for AGE format.

See these links for more details:
 - https://age-encryption.org/v1
 - https://github.com/FiloSottile/age
 - https://github.com/str4d/rage/
"""

import argparse
import base64
import contextlib
import datetime
import logging
import sys
import traceback

import bech32
import pkg_resources
import semver
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from .. import device, server, util
from . import client

log = logging.getLogger(__name__)


def bech32_decode(prefix, encoded):
    """Decode Bech32-encoded data."""
    hrp, data = bech32.bech32_decode(encoded)
    assert prefix == hrp
    return bytes(bech32.convertbits(data, 5, 8, pad=False))


def bech32_encode(prefix, data):
    """Encode data using Bech32."""
    return bech32.bech32_encode(prefix, bech32.convertbits(bytes(data), 8, 5))


def run_pubkey(device_type, args):
    """Initialize hardware-based GnuPG identity."""
    util.setup_logging(verbosity=args.verbose)
    log.warning('This AGE tool is still in EXPERIMENTAL mode, '
                'so please note that the API and features may '
                'change without backwards compatibility!')

    c = client.Client(device=device_type())
    pubkey = c.pubkey(identity=client.create_identity(args.identity), ecdh=True)
    recipient = bech32_encode(prefix="age", data=pubkey)
    print(f"# recipient: {recipient}")
    print(f"# SLIP-0017: {args.identity}")
    data = args.identity.encode()
    encoded = bech32_encode(prefix="age-plugin-trezor-", data=data).upper()
    decoded = bech32_decode(prefix="age-plugin-trezor-", encoded=encoded)
    assert decoded.startswith(data)
    print(encoded)


def base64_decode(encoded: str) -> bytes:
    """Decode Base64-encoded data (after padding correctly with '=')."""
    k = len(encoded) % 4
    pad = (4 - k) if k else 0
    return base64.b64decode(encoded + ("=" * pad))


def base64_encode(data: bytes) -> str:
    """Encode data using Base64 (and remove '=')."""
    return base64.b64encode(data).replace(b"=", b"").decode()


def decrypt(key, encrypted):
    """Decrypt age-encrypted data."""
    cipher = ChaCha20Poly1305(key)
    try:
        return cipher.decrypt(
            nonce=(b"\x00" * 12),
            data=encrypted,
            associated_data=None)
    except InvalidTag:
        return None


def run_decrypt(device_type, args):
    """Unlock hardware device (for future interaction)."""
    c = client.Client(device=device_type())

    lines = (line.strip() for line in sys.stdin)  # strip whitespace
    lines = (line for line in lines if line)  # skip empty lines

    identity = None
    for line in lines:
        if line == "-> done":
            break

        if line.startswith("-> add-identity "):
            encoded = line.split(" ")[-1].lower()
            data = bech32_decode("age-plugin-trezor-", encoded)
            assert identity is None, identity
            identity = client.create_identity(data.decode())

        elif line.startswith("-> recipient-stanza "):
            file_index, tag, *args = line.split(" ")[2:]
            body = next(lines)
            if tag != "X25519":
                continue

            peer_pubkey = base64_decode(args[0])
            encrypted = base64_decode(body)
            key = c.ecdh(identity=identity, peer_pubkey=peer_pubkey)
            result = decrypt(key=key, encrypted=encrypted)
            if not result:
                continue

            sys.stdout.write(f'-> file-key {file_index}\n{base64_encode(result)}\n-> done\n\n')
            sys.stdout.flush()
            sys.stdout.close()
            break


def main(device_type):
    """Parse command-line arguments."""
    p = argparse.ArgumentParser()

    agent_package = device_type.package_name()
    resources_map = {r.key: r for r in pkg_resources.require(agent_package)}
    resources = [resources_map[agent_package], resources_map['libagent']]
    versions = '\n'.join('{}={}'.format(r.key, r.version) for r in resources)
    p.add_argument('--version', help='print the version info',
                   action='version', version=versions)

    p.add_argument('-i', '--identity')
    p.add_argument('-v', '--verbose', default=0, action='count')
    p.add_argument('--age-plugin')

    args = p.parse_args()

    logging.basicConfig(
        filename="/tmp/debug.log", level="DEBUG",
        format='%(asctime)s %(levelname)-12s %(message)-100s [%(filename)s:%(lineno)d]')
    log.debug("starting age plugin: %s", args)

    device_type.ui = device.ui.UI(device_type=device_type, config=vars(args))

    try:
        if args.identity:
            run_pubkey(device_type=device_type, args=args)
        elif args.age_plugin:
            run_decrypt(device_type=device_type, args=args)
    except Exception:  # pylint: disable=broad-except
        log.expection("age plugin failed")

    log.debug("closing age plugin")
