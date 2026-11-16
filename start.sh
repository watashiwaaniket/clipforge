#!/usr/bin/env bash
# One-command local boot: compose stack in the background, Next.js in the foreground.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required." >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "Node.js 18+ (npm) is required for the frontend." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

python3 - <<'PY'
import pathlib, base64, os, secrets
path = pathlib.Path(".env")
text = path.read_text()
lines = text.splitlines()

def fill(key: str, value: str) -> None:
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            current = line.split("=", 1)[1].strip()
            # Drop inline comments left on blank example values.
            if current == "" or current.startswith("#"):
                lines[i] = f"{key}={value}"
            return
    lines.append(f"{key}={value}")

fernet = base64.urlsafe_b64encode(os.urandom(32)).decode()
fill("ENCRYPTION_KEY", fernet)
fill("JWT_SECRET", secrets.token_hex(32))
path.write_text("\n".join(lines) + "\n")
print("Ensured ENCRYPTION_KEY and JWT_SECRET are set in .env (existing values kept).")
PY

docker compose up --build -d --wait

ok=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if docker compose exec -T api alembic upgrade head; then
    ok=1
    break
  fi
  sleep 3
done
if [[ "$ok" -ne 1 ]]; then
  echo "alembic upgrade head failed" >&2
  exit 1
fi

cd frontend
if [[ ! -d node_modules ]]; then
  npm install
fi
exec npm run dev
