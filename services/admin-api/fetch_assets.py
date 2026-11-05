"""Derleme anında: DB-IP Lite ülke veritabanı (CC BY 4.0) ve flag-icons SVG bayrakları (MIT).
İndirme başarısız olursa derleme durmaz; panel ülke sekmesini "GeoIP yok" olarak gösterir."""
import datetime as dt
import gzip
import io
import tarfile
import urllib.request
from pathlib import Path

GEO = Path("/srv/geo/country.mmdb")
FLAGS = Path("/srv/flags")
FLAG_PKG = "https://registry.npmjs.org/flag-icons/-/flag-icons-7.2.3.tgz"


def get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "hp-build"}), timeout=60) as r:
        return r.read()


def geo() -> None:
    d = dt.date.today().replace(day=1)
    for m in (d, (d - dt.timedelta(days=1)).replace(day=1)):  # ayın dosyası henüz yayımlanmamış olabilir
        try:
            data = gzip.decompress(get(f"https://download.db-ip.com/free/dbip-country-lite-{m:%Y-%m}.mmdb.gz"))
        except Exception as e:
            print(f"geo {m:%Y-%m}: {e}")
            continue
        GEO.parent.mkdir(parents=True, exist_ok=True)
        GEO.write_bytes(data)
        print(f"geo {m:%Y-%m}: {len(data)} bytes")
        return
    print("WARNING: GeoIP database unavailable")


def flags() -> None:
    try:
        tf = tarfile.open(fileobj=io.BytesIO(get(FLAG_PKG)), mode="r:gz")
    except Exception as e:
        print(f"WARNING: flags unavailable: {e}")
        return
    FLAGS.mkdir(parents=True, exist_ok=True)
    n = 0
    for m in tf.getmembers():
        p = Path(m.name)
        if m.isfile() and p.parent.as_posix() == "package/flags/4x3" and p.suffix == ".svg" and len(p.stem) == 2:
            (FLAGS / p.name).write_bytes(tf.extractfile(m).read())
            n += 1
    print(f"flags: {n}")


geo()
flags()
