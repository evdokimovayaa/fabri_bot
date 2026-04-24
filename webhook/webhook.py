"""GitHub webhook receiver — auto-deploys on push to main."""

import hashlib
import hmac
import logging
import os
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("WEBHOOK_PORT", 9000))
SECRET = os.environ.get("WEBHOOK_SECRET", "")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("webhook")


def deploy():
    """Execute git pull and docker compose restart."""
    try:
        logger.info("Starting deployment...")

        # Git pull
        logger.info("Running: git pull origin main")
        result = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode != 0:
            logger.error(f"Git pull failed: {result.stderr}")
            return False
        logger.info(f"Git pull output: {result.stdout}")

        # Docker compose restart
        logger.info("Running: docker compose up -d --build")
        result = subprocess.run(
            ["docker", "compose", "up", "-d", "--build"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=300
        )
        if result.returncode != 0:
            logger.error(f"Docker compose failed: {result.stderr}")
            return False
        logger.info(f"Docker compose output: {result.stdout}")

        logger.info("✅ Deployment completed successfully")
        return True

    except subprocess.TimeoutExpired:
        logger.error("Deployment command timed out")
        return False
    except Exception as e:
        logger.error(f"Deployment error: {e}")
        return False


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        logger.info(f"Webhook received from {self.client_address[0]}")

        # Verify signature if SECRET is set
        if SECRET:
            signature = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.HMAC(
                SECRET.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                logger.warning("Signature verification failed")
                self.send_response(403)
                self.end_headers()
                return
            logger.info("Signature verified ✓")

        # Check branch
        try:
            import json
            payload = json.loads(body)
            ref = payload.get("ref", "")
            branch = ref.split("/")[-1]

            if branch != "main":
                logger.info(f"Ignoring push to branch '{branch}' (only 'main' triggers deploy)")
                self.send_response(200)
                self.end_headers()
                return

            logger.info(f"Push to 'main' detected, starting deployment...")
        except Exception as e:
            logger.warning(f"Could not parse webhook payload: {e}")

        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "deployment queued"}')

        # Run deployment in background
        import threading
        thread = threading.Thread(target=deploy, daemon=True)
        thread.start()

    def log_message(self, format, *args):
        # Suppress default logging
        pass


if __name__ == "__main__":
    logger.info(f"Webhook server listening on 0.0.0.0:{PORT}")
    logger.info(f"Project directory: {PROJECT_DIR}")
    logger.info(f"Signature verification: {'enabled' if SECRET else 'disabled'}")

    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    server.serve_forever()
