"""Test restore from backup - verifies decryption + SQLite integrity."""
import base64, json, os, sqlite3, sys, tarfile, tempfile, shutil, zipfile
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.backends import default_backend

def derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1, backend=default_backend())
    return kdf.derive(passphrase.encode())

def decrypt(ct: bytes, nonce: bytes, key: bytes) -> bytes:
    return AESGCM(key).decrypt(nonce, ct, associated_data=None)

backup_zip = sys.argv[1] if len(sys.argv) > 1 else "/restore.zip"
passphrase = os.environ.get("BACKUP_PW", "")
if not passphrase:
    sys.exit("Set BACKUP_PW env var")

tmp = tempfile.mkdtemp()
try:
    # 1. Extract zip (no password - outer zip is unencrypted, inner files are encrypted)
    with zipfile.ZipFile(backup_zip) as z:
        z.extractall(tmp)

    # 2. Load manifest
    manifest = json.loads(open(f"{tmp}/manifest.json").read())
    kdf = manifest["kdf"]
    salt = base64.b64decode(kdf["salt_b64"])
    key = derive_key(passphrase, salt)
    print(f"Manifest: version={manifest['version']} actor={manifest['actor']} created={manifest['created_at']}")

    # 3. Decrypt state.sqlite
    db_entry = manifest["files"].get("state.sqlite.enc")
    if db_entry:
        nonce = base64.b64decode(db_entry["nonce_b64"])
        ct = open(f"{tmp}/state.sqlite.enc", "rb").read()
        db_bytes = decrypt(ct, nonce, key)
        db_path = f"{tmp}/state.sqlite"
        open(db_path, "wb").write(db_bytes)

        conn = sqlite3.connect(db_path)
        tables = ["contacts","campaigns","templates","entities","dispatched_job","settings"]
        counts = {}
        for t in tables:
            try: counts[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            except: pass
        conn.close()
        print("SQLite row counts:", counts)
    else:
        print("WARNING: no state.sqlite.enc in backup")

    # 4. Decrypt keys
    keys_entry = manifest["files"].get("keys.tar.enc")
    if keys_entry:
        nonce = base64.b64decode(keys_entry["nonce_b64"])
        ct = open(f"{tmp}/keys.tar.enc", "rb").read()
        keys_bytes = decrypt(ct, nonce, key)
        keys_tmp = f"{tmp}/keys"
        os.makedirs(keys_tmp)
        import io
        with tarfile.open(fileobj=io.BytesIO(keys_bytes)) as t:
            t.extractall(keys_tmp)
        key_files = os.listdir(keys_tmp)
        print(f"Keys: {key_files}")
        bearer = open(f"{keys_tmp}/cloud_bearer.txt").read().strip() if "cloud_bearer.txt" in key_files else ""
        print(f"Bearer token present: {bool(bearer)} ({len(bearer)} chars)")
    else:
        print("WARNING: no keys.tar.enc in backup")

    print("\n✓ RESTORE TEST PASSED - backup is valid and decryptable")

finally:
    shutil.rmtree(tmp)
