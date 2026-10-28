"""Budowanie strony i klienta dla Vercel (vercel.json): strona na /, klient na /app/."""

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_vercel_build_puts_the_site_at_root_and_the_client_under_app(tmp_path):
    cfg = json.loads((ROOT / "vercel.json").read_text())
    shutil.copytree(ROOT / "docs" / "site", tmp_path / "docs" / "site")
    shutil.copytree(ROOT / "host" / "api" / "static", tmp_path / "host" / "api" / "static")
    subprocess.run(["sh", "-c", cfg["buildCommand"]], cwd=tmp_path, check=True, capture_output=True)
    out = tmp_path / cfg["outputDirectory"]
    assert "Asystent głosowy po polsku" in (out / "index.html").read_text()
    for name in ("index.html", "app.js", "protocol.js", "wave.js", "style.css", "sw.js", "manifest.json",
                 "mic-worklet.js", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"):
        assert (out / "app" / name).is_file(), name
    manifest = json.loads((out / "app" / "manifest.json").read_text())
    assert manifest["start_url"] == "./" and manifest["scope"] == "./"  # względne: działa pod /app/
    assert (out / "app" / "index.html").read_bytes() == (ROOT / "host" / "api" / "static" / "index.html").read_bytes()
    # ścieżki plików w sw.js i manifeście to nazwy względne, bo klient leży pod /app/
    assert 'start_url": "/' not in (out / "app" / "manifest.json").read_text()


def test_vercel_config_is_a_static_site():
    cfg = json.loads((ROOT / "vercel.json").read_text())
    assert cfg["framework"] is None and cfg["outputDirectory"] == "public"
    assert any(h["source"] == "/app/sw.js" for h in cfg["headers"])
