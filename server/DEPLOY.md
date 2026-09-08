# Deploying the MithraVoice license server (Ubuntu, existing MySQL)

Domain: **key.mithravoice.mithracorp.com** — point an A record at this
box before step 6.

Assumes MySQL is already installed and running (per your setup) and you
have a fresh Ubuntu 22.04/24.04 box for the API + nginx.

## 1. System packages

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv nginx certbot python3-certbot-nginx
```

(No MySQL install step — you've already got that running.)

## 2. Database

Run this against your existing MySQL server:

```bash
mysql -u root -p -e "
  CREATE USER IF NOT EXISTS 'mithravoice'@'localhost' IDENTIFIED BY 'CHANGE_ME';
  GRANT ALL PRIVILEGES ON mithravoice.* TO 'mithravoice'@'localhost';
  FLUSH PRIVILEGES;
"
mysql -u root -p < server/schema.sql
```

`schema.sql` creates the `mithravoice` database itself (`CREATE DATABASE
IF NOT EXISTS`), so you don't need a separate `CREATE DATABASE` step —
just make sure the `mithravoice` MySQL user above has privileges on it.

If your MySQL server is on a different host than the API, swap
`'mithravoice'@'localhost'` for `'mithravoice'@'%'` (or the API box's
specific IP) and adjust `DATABASE_URL`'s host in step 3 accordingly.

## 3. App setup

```bash
cd /opt
sudo git clone <your repo> mithravoice   # or scp the server/ folder up
cd mithravoice/server
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: set DATABASE_URL's password to match step 2
```

`requirements.txt` uses `pymysql` (pure Python, no compiled MySQL client
needed) plus `cryptography` (required for MySQL 8's default
`caching_sha2_password` auth plugin).

## 4. Generate the production RSA keypair

**Do this on the server, not on a dev machine — never let the private key touch git or a laptop.**

```bash
python - <<'EOF'
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
with open("keys/mithravoice_private_key.pem", "wb") as f:
    f.write(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
with open("keys/mithravoice_public_key.pem", "wb") as f:
    f.write(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
print("done")
EOF
chmod 600 keys/mithravoice_private_key.pem
```

Copy `keys/mithravoice_public_key.pem` back down to the **client**
project at `mithracorp/keys/mithravoice_public_key.pem` before you build
the Windows installer — that's what lets the app verify tokens offline.
The demo keypair checked into this project is for local testing only;
replace it before shipping.

## 5. systemd service

`/etc/systemd/system/mithravoice.service`:

```ini
[Unit]
Description=MithraVoice License Server
After=network.target mysql.service

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/mithravoice/server
EnvironmentFile=/opt/mithravoice/server/.env
ExecStart=/opt/mithravoice/server/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mithravoice
sudo systemctl status mithravoice
```

## 6. nginx + TLS for key.mithravoice.mithracorp.com

`/etc/nginx/sites-available/mithravoice`:

```nginx
server {
    listen 80;
    server_name key.mithravoice.mithracorp.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/mithravoice /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d key.mithravoice.mithracorp.com
```

## 7. Issue your first key

```bash
cd /opt/mithravoice/server
source venv/bin/activate
python create_license_key.py --email you@example.com --plan solo_monthly --months 1
```

## 8. Point the client at it

The client's `config.py` already defaults `LICENSE_SERVER_URL` to
`https://key.mithravoice.mithracorp.com` — no `.env` override needed
unless you're testing against a different host.

## Smoke test

```bash
curl https://key.mithravoice.mithracorp.com/healthz
curl -X POST https://key.mithravoice.mithracorp.com/v1/activate \
  -H "Content-Type: application/json" \
  -d '{"key_code":"MVCE-XXXX-XXXX-XXXX","device_fingerprint":"test123","hostname":"test","os":"linux"}'
```

Every route above (`/v1/activate`, `/v1/heartbeat`, `/v1/deactivate`,
seat-limit enforcement, and `create_license_key.py`) has been tested
end-to-end against a real MySQL instance during development — this
should work as-is against your server once the domain and password are
in place.
