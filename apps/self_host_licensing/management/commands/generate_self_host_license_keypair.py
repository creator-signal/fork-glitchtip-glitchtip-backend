import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Generate an Ed25519 keypair for signing self-host license blobs. "
        "Prints the private key (for SELF_HOST_LICENSE_SIGNING_KEY on the "
        "issuer deployment) and the public key (to add to TRUSTED_PUBLIC_KEYS "
        "in apps/self_host_licensing/keys.py)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--kid", default="v1", help="Key id to label this pair")

    def handle(self, *args, **options):
        kid = options["kid"]
        private = Ed25519PrivateKey.generate()
        public = private.public_key()
        priv_b64 = base64.b64encode(
            private.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
        ).decode()
        pub_b64 = base64.b64encode(
            public.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ).decode()

        self.stdout.write(f"kid:               {kid}")
        self.stdout.write(f"private (SECRET):  {priv_b64}")
        self.stdout.write(f"public:            {pub_b64}")
        self.stdout.write("")
        self.stdout.write("Issuer deployment env:")
        self.stdout.write(f"  SELF_HOST_LICENSE_SIGNING_KID={kid}")
        self.stdout.write(f"  SELF_HOST_LICENSE_SIGNING_KEY={priv_b64}")
        self.stdout.write("")
        self.stdout.write(
            "Add this tuple to TRUSTED_PUBLIC_KEYS in apps/self_host_licensing/keys.py:"
        )
        self.stdout.write(f'    ("{kid}", "{pub_b64}"),')
