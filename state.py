"""Encrypted private state for GitHub Actions. Subscriber chat ids (including stop tombstones), the Telegram
offset, webhook ack keys, and the outbox never sit in the public repo in plain text: they live in
state/private.enc (AES-256-CBC, PBKDF2-SHA256,
200k iterations, via openssl) with an HMAC-SHA256 check in state/private.mac.
Key: env SIGNALS_STATE_KEY (an Actions secret). Never printed.
  python3 state.py unpack   state/private.enc -> private/*
  python3 state.py pack     private/* -> state/private.enc (rewritten only when the contents changed)"""
import hashlib, hmac, json, os, subprocess, sys
import config as C

FILES = ["subscribers.json", "bot_state.json", "outbox.jsonl", "outage", "telegram.json"]
SD = os.path.join(C.DIR, "state")
ENC, MAC = os.path.join(SD, "private.enc"), os.path.join(SD, "private.mac")
OSSL = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-pass", "env:SIGNALS_STATE_KEY"]


def key():
    k = os.environ.get("SIGNALS_STATE_KEY", "")
    if len(k) < 32:
        sys.exit("state: SIGNALS_STATE_KEY missing")
    return k.encode()


def mac(blob):
    return hmac.new(key(), blob, hashlib.sha256).hexdigest()


def pack():
    blob = json.dumps({f: open(os.path.join(C.PRIVATE_DIR, f)).read() for f in FILES
                       if os.path.exists(os.path.join(C.PRIVATE_DIR, f))}, sort_keys=True).encode()
    m = mac(blob)
    if os.path.exists(MAC) and os.path.exists(ENC) and open(MAC).read().strip() == m:
        return
    os.makedirs(SD, exist_ok=True)
    subprocess.run(OSSL + ["-salt", "-out", ENC], input=blob, check=True)
    open(MAC, "w").write(m + "\n")


def unpack():
    os.makedirs(C.PRIVATE_DIR, mode=0o700, exist_ok=True)
    if not os.path.exists(ENC):
        print("state: no saved state yet")
        return
    blob = subprocess.run(OSSL + ["-d", "-in", ENC], capture_output=True, check=True).stdout
    if not hmac.compare_digest(mac(blob), open(MAC).read().strip()):
        sys.exit("state: integrity check failed")
    for f, txt in json.loads(blob).items():
        if f in FILES:
            open(os.path.join(C.PRIVATE_DIR, f), "w").write(txt)


if __name__ == "__main__":
    {"pack": pack, "unpack": unpack}[sys.argv[1]]()
