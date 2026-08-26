"""Local fixture server for the offline integration test.

It plays two roles on one port:
  * serves the sample images under /img/<name>
  * emulates the two SerpApi endpoints the pipeline uses (POST /image and
    GET /search.json) with responses shaped exactly like the real API, whose
    "visual matches" point back at the served sample images.

This is test infrastructure only - production code always talks to the real
serpapi.com (see SERPAPI_BASE in verified/search/engines/serpapi_engines.py).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def make_handler(base_url: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):  # silence
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path.startswith("/image"):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                return self._json({"message": "Image uploaded successfully.", "image_id": "fixture-image-id"})
            self._json({"error": "unknown"}, 404)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path.startswith("/img/"):
                p = SAMPLES / u.path[5:]
                if not p.exists():
                    self.send_response(404)
                    self.end_headers()
                    return
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png" if p.suffix == ".png" else "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/search.json":
                q = parse_qs(u.query)
                engine = q.get("engine", [""])[0]
                if engine == "google_lens":
                    return self._json(
                        {
                            "search_metadata": {"status": "Success", "id": "fixture"},
                            "search_parameters": {"engine": "google_lens"},
                            "visual_matches": [
                                {"position": 1, "title": "Barack Obama (@barackobama) • Instagram photos", "link": "https://www.instagram.com/p/FIXTURE1/", "source": "Instagram", "thumbnail": f"{base_url}/img/obama2.jpg", "image": f"{base_url}/img/obama2.jpg", "image_width": 626, "image_height": 1200},
                                {"position": 2, "title": "Joe Biden on X", "link": "https://x.com/JoeBiden/status/1", "source": "X", "thumbnail": f"{base_url}/img/biden.jpg", "image": f"{base_url}/img/biden.jpg"},
                                {"position": 3, "title": "Group photo", "link": "https://example.com/group", "source": "example.com", "thumbnail": f"{base_url}/img/t1.jpg", "image": f"{base_url}/img/t1.jpg"},
                                {"position": 4, "title": "Two people", "link": "https://www.facebook.com/photo/?fbid=1", "source": "Facebook", "thumbnail": f"{base_url}/img/two_people.jpg", "image": f"{base_url}/img/two_people.jpg"},
                                {"position": 5, "title": "Broken image", "link": "https://www.pinterest.com/pin/1/", "source": "Pinterest", "thumbnail": f"{base_url}/img/missing.jpg", "image": f"{base_url}/img/missing.jpg"},
                            ],
                            "knowledge_graph": {"title": "Barack Obama"},
                        }
                    )
                return self._json({"error": f"engine {engine} not emulated"}, 400)
            self._json({"error": "not found"}, 404)

    return H


class FixtureServer:
    def __init__(self, port: int = 8765):
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.base_url))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


if __name__ == "__main__":
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    with FixtureServer(port) as fx:
        print("fixture server on", fx.base_url, flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
