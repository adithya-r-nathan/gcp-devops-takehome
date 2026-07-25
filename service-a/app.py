import os
import requests
import google.auth.transport.requests
import google.oauth2.id_token
from flask import Flask, jsonify

app = Flask(__name__)
BACKEND_URL = os.environ["BACKEND_URL"]


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/")
def index():
    auth_req = google.auth.transport.requests.Request()
    token = google.oauth2.id_token.fetch_id_token(auth_req, BACKEND_URL)
    r = requests.get(
        f"{BACKEND_URL}/data",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    return jsonify(frontend="ok", backend_response=r.json())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
