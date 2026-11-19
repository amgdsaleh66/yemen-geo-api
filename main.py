import os
import re
import json
import math
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query, Request, Response, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from pmtiles.reader import Reader, MmapSource
from shapely.geometry import shape, Point
from shapely.prepared import prep

# ==========================================
# 1. إعدادات النظام والحماية الاختيارية
# ==========================================
DATA_DIR = "data"
PMTILES_PATH = os.path.join(DATA_DIR, "yemen_full.pmtiles")
PLACES_PATH = os.path.join(DATA_DIR, "yem_places_full.geojsonl")
ADM1_PATH = os.path.join(DATA_DIR, "yem_admin1.geojson")
ADM2_PATH = os.path.join(DATA_DIR, "yem_admin2.geojson")
DB_PATH = os.path.join(DATA_DIR, "places_search.db")

# اتركه فارغاً لتعطيل الحماية، أو ضع مفتاحاً لتفعيلها
API_KEY = os.getenv("YEMEN_API_KEY", "")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(key: str = Security(api_key_header)):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=403, detail="مفتاح API غير صالح أو مفقود")
    return key

# ==========================================
# 2. تطبيع النصوص العربية للبحث الدقيق
# ==========================================
def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"[\u064B-\u065F\u0670]", "", text)  # إزالة التشكيل
    text = re.sub(r"[إأآا]", "ا", text)               # توحيد الألف
    text = re.sub(r"ى", "ي", text)                    # توحيد الياء
    text = re.sub(r"ؤ", "ء", text)
    text = re.sub(r"ئ", "ء", text)
    text = re.sub(r"ة", "ه", text)                    # توحيد التاء المربوطة
    return text.strip().lower()

# ==========================================
# 3. تهيئة قاعدة بيانات البحث والحدود الإدارية
# ==========================================
pmtiles_file = None
tile_reader = None
admin1_polygons = []
admin2_polygons = []

def init_search_db():
    if os.path.exists(DB_PATH):
        return
    print("جاري بناء فهرس البحث السريع (SQLite FTS5) لأول مرة...")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE places (
            id INTEGER PRIMARY KEY,
            name_ar TEXT,
            name_en TEXT,
            norm_ar TEXT,
            fclass TEXT,
            fcode TEXT,
            population INTEGER,
            lng REAL,
            lat REAL
        )
    """)
    cur.execute("""
        CREATE VIRTUAL TABLE places_fts USING fts5(
            name_ar, name_en, norm_ar, content='places', content_rowid='id'
        )
    """)
    with open(PLACES_PATH, "r", encoding="utf-8") as f:
        batch = []
        for line in f:
            feat = json.loads(line)
            p = feat["properties"]
            lng, lat = feat["geometry"]["coordinates"]
            batch.append((
                p["id"], p["name_ar"], p["name_en"],
                normalize_arabic(p["name_ar"]),
                p["fclass"], p["fcode"], p.get("population", 0),
                lng, lat
            ))
            if len(batch) >= 5000:
                cur.executemany("INSERT OR IGNORE INTO places VALUES (?,?,?,?,?,?,?,?,?)", batch)
                batch.clear()
        if batch:
            cur.executemany("INSERT OR IGNORE INTO places VALUES (?,?,?,?,?,?,?,?,?)", batch)

    cur.execute("INSERT INTO places_fts(rowid, name_ar, name_en, norm_ar) SELECT id, name_ar, name_en, norm_ar FROM places")
    cur.execute("CREATE INDEX idx_places_coords ON places(lat, lng)")
    conn.commit()
    conn.close()
    print("تم بناء فهرس البحث بنجاح.")

def load_admin_polygons(filepath: str):
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    loaded = []
    for feat in data.get("features", []):
        geom = shape(feat["geometry"])
        loaded.append({
            "prepared": prep(geom),
            "properties": feat["properties"]
        })
    return loaded

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pmtiles_file, tile_reader, admin1_polygons, admin2_polygons
    pmtiles_file = open(PMTILES_PATH, "rb")
    tile_reader = Reader(MmapSource(pmtiles_file))
    init_search_db()
    admin1_polygons = load_admin_polygons(ADM1_PATH)
    admin2_polygons = load_admin_polygons(ADM2_PATH)
    yield
    if pmtiles_file:
        pmtiles_file.close()

app = FastAPI(
    title="Yemen Geographic & Vector Tiles API",
    description="واجهة برمجية متكاملة لخرائط اليمن، البحث الجغرافي، وتحديد المواقع الإدارية",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ==========================================
# 4. مسارات الخرائط المتجهة (Vector Tiles)
# ==========================================
@app.get("/tilejson.json", dependencies=[Security(verify_api_key)])
def get_tilejson(request: Request):
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    base_url = f"{proto}://{host}"
    # ... باقي الدالة كما هي
    header = tile_reader.header()
    metadata = tile_reader.metadata()
    return {
        "tilejson": "3.0.0",
        "name": "Yemen Full Vector Map",
        "tiles": [f"{base_url}/tiles/{{z}}/{{x}}/{{y}}.pbf"],
        "minzoom": header["min_zoom"],
        "maxzoom": header["max_zoom"],
        "bounds": [
            header["min_lon_e7"] / 1e7,
            header["min_lat_e7"] / 1e7,
            header["max_lon_e7"] / 1e7,
            header["max_lat_e7"] / 1e7,
        ],
        "vector_layers": metadata.get("vector_layers", [])
    }

@app.get("/tiles/{z}/{x}/{y}.pbf", dependencies=[Security(verify_api_key)])
def get_tile(z: int, x: int, y: int):
    tile_data = tile_reader.get(z, x, y)
    if not tile_data:
        return Response(status_code=204)
    return Response(
        content=tile_data,
        media_type="application/x-protobuf",
        headers={
            "Content-Encoding": "gzip",
            "Cache-Control": "public, max-age=86400"
        }
    )

# ==========================================
# 5. مسار البحث الذكي (Geocoding Search)
# ==========================================
@app.get("/api/search", dependencies=[Security(verify_api_key)])
def search_places(
    q: str = Query(..., min_length=2, description="اسم المدينة، القرية، الجبل، أو الوادي"),
    fclass: Optional[str] = Query(None, description="تصنيف المعلم: P (سكان), T (جبال), H (مياه), S (أثار)"),
    limit: int = Query(15, ge=1, le=50)
):
    norm_q = normalize_arabic(q)
    fts_query = f'"{norm_q}"* OR "{q.strip()}"*'

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    sql = """
        SELECT p.id, p.name_ar, p.name_en, p.fclass, p.fcode, p.population, p.lng, p.lat
        FROM places_fts f
        JOIN places p ON p.id = f.rowid
        WHERE places_fts MATCH ?
    """
    params = [fts_query]

    if fclass:
        sql += " AND p.fclass = ?"
        params.append(fclass.upper())

    sql += " ORDER BY p.population DESC, rank LIMIT ?"
    params.append(limit)

    rows = cur.execute(sql, params).fetchall()
    conn.close()

    return {
        "query": q,
        "count": len(rows),
        "results": [dict(r) for r in rows]
    }

# ==========================================
# 6. الترميز العكسي (Reverse Geocoding)
# ==========================================
def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))

@app.get("/api/reverse", dependencies=[Security(verify_api_key)])
def reverse_geocode(
    lat: float = Query(..., ge=11.5, le=19.5, description="خط العرض"),
    lng: float = Query(..., ge=41.0, le=55.0, description="خط الطول")
):
    pt = Point(lng, lat)

    # تحديد المحافظة والمديرية
    governorate = next((a["properties"] for a in admin1_polygons if a["prepared"].contains(pt)), None)
    district = next((a["properties"] for a in admin2_polygons if a["prepared"].contains(pt)), None)

    # البحث عن أقرب 5 معالم ضمن نطاق ~15 كم
    delta = 0.15
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    candidates = conn.execute("""
        SELECT id, name_ar, name_en, fclass, population, lng, lat
        FROM places
        WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?
    """, (lat - delta, lat + delta, lng - delta, lng + delta)).fetchall()
    conn.close()

    nearest = []
    for c in candidates:
        dist = round(haversine_km(lat, lng, c["lat"], c["lng"]), 2)
        item = dict(c)
        item["distance_km"] = dist
        nearest.append(item)

    nearest.sort(key=lambda x: x["distance_km"])

    return {
        "coordinates": {"lat": lat, "lng": lng},
        "governorate": governorate,
        "district": district,
        "nearest_places": nearest[:5]
    }