"""python3 serve.py  →  http://localhost:8081/viewer.html"""
import http.server, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        super().end_headers()
    def do_OPTIONS(self):
        self.send_response(200); self.end_headers()
    def log_message(self, fmt, *args):
        pass

Handler.extensions_map.update({
    ".pnts": "application/octet-stream",
    ".json": "application/json",
})

PORT = 8081
print(f"Graph tiles at http://localhost:{PORT}/")
print(f"  viewer → http://localhost:{PORT}/viewer.html")
print(f"  tiles  → http://localhost:{PORT}/tileset.json")
http.server.HTTPServer(("", PORT), Handler).serve_forever()
