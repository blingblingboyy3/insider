"""Make the push key pair once:  python scanner/keys.py
Prints the three repo secrets to set. The private key never leaves GitHub's secret store."""
import base64, sys
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

k = ec.generate_private_key(ec.SECP256R1())
pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
raw = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
pub = base64.urlsafe_b64encode(raw).decode().rstrip("=")
if "--json" in sys.argv:
    import json; print(json.dumps({"VAPID_PRIVATE_KEY": pem, "VAPID_PUBLIC_KEY": pub}))
else:
    print("VAPID_PUBLIC_KEY=" + pub); print("VAPID_PRIVATE_KEY=\n" + pem)
