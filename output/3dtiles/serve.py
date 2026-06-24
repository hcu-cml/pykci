"""python3 serve.py  →  http://localhost:8080/viewer.html"""
import http.server, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Allow file:// and any other origin to fetch tiles (CORS)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # suppress per-request noise

Handler.extensions_map.update({
    '.gltf': 'model/gltf+json',
    '.glb':  'model/gltf-binary',
    '.json': 'application/json',
})

print("Serving 3D Tiles at http://localhost:8080/")
print("  tileset → http://localhost:8080/tileset.json")
http.server.HTTPServer(('', 8080), Handler).serve_forever()
