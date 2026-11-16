import os
import sqlite3
import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
DB_PATH = Path(os.environ.get("ROUTECOST_DB", BASE / "routecost.db"))
ROUTE_PROVIDER_URL = os.environ.get("ROUTECOST_ROUTER_URL", "https://router.project-osrm.org/route/v1/driving")
GEOCODER_URL = os.environ.get("ROUTECOST_GEOCODER_URL", "https://nominatim.openstreetmap.org/search")
OVERPASS_URL = os.environ.get("ROUTECOST_OVERPASS_URL", "https://overpass.private.coffee/api/interpreter")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
GEOCODE_CACHE = {}
GEOCODE_LAST = 0.0


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS trips (
          id INTEGER PRIMARY KEY AUTOINCREMENT, trip_name TEXT NOT NULL, start_location TEXT NOT NULL,
          destinations TEXT NOT NULL, distance REAL NOT NULL, duration REAL NOT NULL,
          vehicle_type TEXT NOT NULL, fuel_average REAL NOT NULL, fuel_price REAL NOT NULL,
          travellers INTEGER NOT NULL, currency TEXT NOT NULL, expenses TEXT NOT NULL,
          total_cost REAL NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS geocode_cache (query TEXT PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, label TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS hotel_cache (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL NOT NULL);
        """)


def validate_trip(data):
    if not isinstance(data, dict):
        raise ValueError("Please submit trip details.")
    name = str(data.get("trip_name", "")).strip()[:100]
    start = str(data.get("start_location", "")).strip()[:160]
    destinations = data.get("destinations", [])
    if not name or not start or not isinstance(destinations, list) or not destinations or len(destinations) > 12:
        raise ValueError("Add a trip name, starting point, and at least one destination (up to 12).")
    destinations = [str(x).strip()[:160] for x in destinations]
    if any(not x for x in destinations):
        raise ValueError("Destination names cannot be blank.")
    try:
        distance = float(data.get("distance", 0))
        average = float(data.get("fuel_average", 0))
        fuel_price = float(data.get("fuel_price", 0))
        travellers = int(data.get("travellers", 1))
        expenses = data.get("expenses", [])
        if not isinstance(expenses, list) or len(expenses) > 80:
            raise ValueError
        expenses = [{"category": str(e.get("category", "Other"))[:32], "description": str(e.get("description", ""))[:120], "amount": float(e.get("amount", 0))} for e in expenses]
    except (TypeError, ValueError, AttributeError):
        raise ValueError("Enter valid numbers for distance, fuel, travellers, and expenses.")
    if distance <= 0 or average <= 0 or fuel_price < 0 or travellers <= 0 or any(e["amount"] < 0 for e in expenses):
        raise ValueError("Distance, mileage, and travellers must be positive. Costs cannot be negative.")
    duration = max(0, float(data.get("duration", 0) or 0))
    currency = str(data.get("currency", "PKR"))[:3].upper()
    total = distance / average * fuel_price + sum(e["amount"] for e in expenses)
    return (name, start, destinations, distance, duration, str(data.get("vehicle_type", "Car"))[:30], average, fuel_price, travellers, currency, expenses, total)


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify(ok=True, routing_mode="osrm_public", accommodation_mode="osm_discovery_only")


@app.post("/api/costs/calculate")
def calculate():
    try:
        trip = validate_trip(request.get_json(silent=True))
        fuel = trip[3] / trip[6]
        return jsonify(distance_km=trip[3], fuel_litres=round(fuel, 2), fuel_cost=round(fuel * trip[7], 2), other_expenses=round(sum(e["amount"] for e in trip[10]), 2), total_cost=round(trip[11], 2), per_person=round(trip[11] / trip[8], 2), currency=trip[9])
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/routes/calculate")
def route_calc():
    data = request.get_json(silent=True) or {}
    start = str(data.get("start", "")).strip()[:160]
    stops = data.get("stops", [])
    if not start or not isinstance(stops, list) or not stops or len(stops) > 12 or any(not str(s).strip() for s in stops):
        return jsonify(error="Enter a starting point and at least one destination."), 400
    try:
        points = [geocode(start)] + [geocode(str(s).strip()[:160]) for s in stops]
        coords = ";".join(f"{p['lon']},{p['lat']}" for p in points)
        url = f"{ROUTE_PROVIDER_URL}/{coords}?overview=full&geometries=geojson&steps=true&alternatives=false"
        result = external_json(url, timeout=25)
        if result.get("code") != "Ok" or not result.get("routes"):
            return jsonify(error="No drivable route was found for these places. Try a nearby city or a more specific address."), 422
        route = result["routes"][0]
        steps = []
        toll_road = False
        for leg in route.get("legs", []):
            for step in leg.get("steps", []):
                name = step.get("name") or "Continue"
                maneuver = step.get("maneuver", {})
                instruction = f"{maneuver.get('type', 'Continue').replace('-', ' ').title()} {maneuver.get('modifier', '')} {name}".strip()
                if step.get("intersections"):
                    toll_road = toll_road or any("toll" in i.get("classes", []) for i in step["intersections"])
                if step.get("distance", 0) > 50:
                    steps.append({"instruction": instruction, "distance_km": round(step["distance"] / 1000, 1)})
        return jsonify(mode="road_route_estimate", distance_km=round(route["distance"] / 1000, 1), duration_minutes=round(route["duration"] / 60), geometry=route["geometry"], points=points, steps=steps[:100], toll_road_detected=toll_road, toll_message="Route data indicates a toll road; toll amount is not provided by this routing service. Add the amount from the official toll schedule." if toll_road else "Toll charges are not included; check the official schedule for your route.", guidance_url="https://www.google.com/maps/dir/" + "/".join(urllib.parse.quote_plus(s) for s in [start] + [str(x) for x in stops]))
    except LookupError as exc:
        return jsonify(error=str(exc)), 422
    except Exception:
        app.logger.exception("Route provider request failed")
        return jsonify(error="The map provider is temporarily unavailable. Please try again shortly."), 502


def external_json(url, timeout=15, payload=None, json_body=False, headers=None):
    body = (json.dumps(payload).encode() if json_body else urllib.parse.urlencode(payload).encode()) if payload is not None else None
    request_headers = {"User-Agent": "RouteCost/1.0 (local trip planner)", "Accept": "application/json", **({"Content-Type": "application/json" if json_body else "application/x-www-form-urlencoded"} if body else {})}
    request_headers.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def price_amount(value):
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    if isinstance(value, dict):
        for key in ("booker_currency", "total", "book", "display", "amount", "accommodation_currency", "value"):
            if key in value:
                result = price_amount(value[key])
                if result is not None:
                    return result
    return None


def geocode(query):
    global GEOCODE_LAST
    key = query.casefold()
    with db() as con:
        row = con.execute("SELECT lat,lon,label FROM geocode_cache WHERE query=?", (key,)).fetchone()
    if row:
        return {"lat": row[0], "lon": row[1], "label": row[2], "query": query}
    # Nominatim policy: only explicit user-triggered searches, at most 1 request/sec.
    wait = 1.05 - (time.monotonic() - GEOCODE_LAST)
    if wait > 0:
        time.sleep(wait)
    q = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 1})
    results = external_json(f"{GEOCODER_URL}?{q}")
    GEOCODE_LAST = time.monotonic()
    if not results:
        raise LookupError(f"Could not find “{query}”. Check the spelling or add a city/country.")
    place = results[0]
    point = {"lat": float(place["lat"]), "lon": float(place["lon"]), "label": place.get("display_name", query), "query": query}
    with db() as con:
        con.execute("INSERT OR REPLACE INTO geocode_cache(query,lat,lon,label) VALUES(?,?,?,?)", (key, point["lat"], point["lon"], point["label"]))
    return point


@app.post("/api/hotels/search")
def hotels_search():
    data = request.get_json(silent=True) or {}
    try:
        lat, lon = float(data["lat"]), float(data["lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Choose a mapped trip stop to search nearby stays."), 400
    try:
        checkin = datetime.strptime(str(data.get("checkin", "")), "%Y-%m-%d").date()
        checkout = datetime.strptime(str(data.get("checkout", "")), "%Y-%m-%d").date()
        travellers = max(1, min(20, int(data.get("travellers", 1))))
        if checkin < date.today() or checkout <= checkin or (checkout - checkin).days > 90 or (checkin - date.today()).days > 500:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify(error="Enter valid future hotel dates (1 to 90 nights, within 500 days) and a traveller count."), 400
    cache_key = f"{lat:.2f},{lon:.2f}:{checkin}:{checkout}:{travellers}"
    booking_token = os.environ.get("BOOKING_DEMAND_API_TOKEN")
    booking_affiliate_id = os.environ.get("BOOKING_AFFILIATE_ID")
    with db() as con:
        cached = con.execute("SELECT payload,fetched_at FROM hotel_cache WHERE cache_key=?", (cache_key,)).fetchone()
    if cached:
        cached_payload = json.loads(cached["payload"])
        cache_ttl = 600 if cached_payload.get("prices_available") else (86400 if not (booking_token and booking_affiliate_id) else 0)
        if time.time() - cached["fetched_at"] < cache_ttl:
            return jsonify(cached_payload)
    booking_base = os.environ.get("BOOKING_DEMAND_API_BASE", "https://demandapi.booking.com/3.3")
    if booking_token and booking_affiliate_id:
        try:
            headers = {"Authorization": f"Bearer {booking_token}", "X-Affiliate-Id": booking_affiliate_id}
            search = {"coordinates": {"latitude": lat, "longitude": lon, "radius": 5}, "booker": {"country": "pk", "platform": "desktop"}, "checkin": checkin.isoformat(), "checkout": checkout.isoformat(), "guests": {"number_of_adults": travellers, "number_of_rooms": 1}, "currency": "PKR", "extras": ["extra_charges", "products"], "rows": 20, "sort": {"by": "price", "direction": "ascending"}}
            results = external_json(f"{booking_base}/accommodations/search", timeout=20, payload=search, json_body=True, headers=headers).get("data", [])
            ids = [item.get("id") for item in results if item.get("id") is not None]
            details = external_json(f"{booking_base}/accommodations/details", timeout=20, payload={"accommodations": ids[:20]}, json_body=True, headers=headers).get("data", []) if ids else []
            detail_by_id = {item.get("id"): item for item in details}
            stays = []
            for item in results:
                detail = detail_by_id.get(item.get("id"), {})
                price_obj = item.get("price", {})
                price = price_amount(price_obj)
                currency_info = item.get("currency", {})
                currency = (currency_info.get("booker") or currency_info.get("accommodation") or "PKR") if isinstance(currency_info, dict) else "PKR"
                location = detail.get("location", {}) or {}
                latlon = detail.get("coordinates", {}) or {}
                stays.append({"name": detail.get("name") or item.get("name") or "Accommodation", "type": "Available stay", "lat": latlon.get("latitude", location.get("latitude", lat)), "lon": latlon.get("longitude", location.get("longitude", lon)), "price": price, "currency": currency, "booking_url": item.get("deep_link_url") or item.get("url") or "https://www.booking.com"})
            response = {"hotels": stays, "prices_available": True, "message": "Live availability and estimated prices for your dates, from Booking.com. Final prices may change."}
            with db() as con:
                con.execute("INSERT OR REPLACE INTO hotel_cache(cache_key,payload,fetched_at) VALUES(?,?,?)", (cache_key, json.dumps(response), time.time()))
            return jsonify(response)
        except Exception:
            app.logger.exception("Booking Demand API request failed; trying OpenStreetMap listings")
    query = f"[out:json][timeout:20];(node(around:5000,{lat},{lon})[tourism~\"hotel|guest_house|hostel|motel\"];way(around:5000,{lat},{lon})[tourism~\"hotel|guest_house|hostel|motel\"];);out center tags 30;"
    try:
        result = external_json(OVERPASS_URL, timeout=12, payload={"data": query})
        hotels = []
        for item in result.get("elements", []):
            tags = item.get("tags", {})
            name = tags.get("name")
            if not name:
                continue
            pos = item.get("center", item)
            search = {"ss": name + " " + str(data.get("label", "")), "group_adults": travellers, "group_children": 0, "no_rooms": 1, "checkin": checkin.isoformat(), "checkout": checkout.isoformat()}
            hotels.append({"name": name, "type": tags.get("tourism", "stay").replace("_", " ").title(), "lat": pos.get("lat"), "lon": pos.get("lon"), "booking_url": "https://www.booking.com/searchresults.html?" + urllib.parse.urlencode(search)})
        response = {"hotels": hotels[:20], "prices_available": False, "message": "Nearby places come from OpenStreetMap. Live availability, ratings, and room prices are shown by Booking.com after you check rooms."}
        with db() as con:
            con.execute("INSERT OR REPLACE INTO hotel_cache(cache_key,payload,fetched_at) VALUES(?,?,?)", (cache_key, json.dumps(response), time.time()))
        return jsonify(response)
    except Exception:
        app.logger.exception("Hotel discovery provider request failed")
        return jsonify(hotels=[], prices_available=False, message="Nearby map listings are temporarily unavailable. Use “See prices on Booking.com” to check stays and live rates for this stop."), 200


@app.route("/api/trips", methods=["GET", "POST"])
def trips():
    if request.method == "GET":
        with db() as con:
            rows = con.execute("SELECT * FROM trips ORDER BY created_at DESC, id DESC").fetchall()
        return jsonify([trip_json(r) for r in rows])
    try:
        t = validate_trip(request.get_json(silent=True))
        with db() as con:
            cur = con.execute("INSERT INTO trips (trip_name,start_location,destinations,distance,duration,vehicle_type,fuel_average,fuel_price,travellers,currency,expenses,total_cost) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (t[0], t[1], __import__('json').dumps(t[2]), t[3], t[4], t[5], t[6], t[7], t[8], t[9], __import__('json').dumps(t[10]), t[11]))
            row = con.execute("SELECT * FROM trips WHERE id=?", (cur.lastrowid,)).fetchone()
        return jsonify(trip_json(row)), 201
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400


def trip_json(row):
    import json
    return {"id": row["id"], "trip_name": row["trip_name"], "start_location": row["start_location"], "destinations": json.loads(row["destinations"]), "distance": row["distance"], "duration": row["duration"], "vehicle_type": row["vehicle_type"], "fuel_average": row["fuel_average"], "fuel_price": row["fuel_price"], "travellers": row["travellers"], "currency": row["currency"], "expenses": json.loads(row["expenses"]), "total_cost": row["total_cost"], "created_at": row["created_at"]}


@app.route("/api/trips/<int:trip_id>", methods=["GET", "PUT", "DELETE"])
def trip_item(trip_id):
    with db() as con:
        row = con.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
        if not row:
            return jsonify(error="Trip not found."), 404
        if request.method == "GET":
            return jsonify(trip_json(row))
        if request.method == "DELETE":
            con.execute("DELETE FROM trips WHERE id=?", (trip_id,))
            return jsonify(ok=True)
        try:
            t = validate_trip(request.get_json(silent=True))
            con.execute("UPDATE trips SET trip_name=?,start_location=?,destinations=?,distance=?,duration=?,vehicle_type=?,fuel_average=?,fuel_price=?,travellers=?,currency=?,expenses=?,total_cost=? WHERE id=?", (t[0], t[1], __import__('json').dumps(t[2]), t[3], t[4], t[5], t[6], t[7], t[8], t[9], __import__('json').dumps(t[10]), t[11], trip_id))
            return jsonify(trip_json(con.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()))
        except (ValueError, TypeError) as exc:
            return jsonify(error=str(exc)), 400


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8081")), debug=os.environ.get("FLASK_DEBUG", "0") == "1")
