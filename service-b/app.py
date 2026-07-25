import os
import hashlib
from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/data")
def data():
    secret = os.environ.get("APP_SECRET", "")  # injected from Secret Manager
    fp = hashlib.sha256(secret.encode()).hexdigest()[:8] if secret else None
    # NEVER return the raw secret
    return jsonify(service="backend", secret_loaded=bool(secret), secret_fingerprint=fp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
