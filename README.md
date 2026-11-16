# RouteCost — Smart Trip Cost Planner

RouteCost is a responsive Flask and SQLite road-trip planner with a map-first itinerary flow, fuel budgeting, user-entered expenses, saved trips, nearby stays, and OpenStreetMap-based road route estimates.

## Run on Windows (PowerShell)

Requires Python 3.10 or newer.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

Open http://127.0.0.1:8081. SQLite initializes automatically in `routecost.db`; saved trips and geocoding results persist across restarts. Set `PORT` or `ROUTECOST_DB` in the environment to customize the port or database path.

## Routing, map, stays, and prices

The planner geocodes entered places through the public OpenStreetMap Nominatim service only when the user requests a route, caches results locally, and asks no more than one uncached geocoding query per second. OSRM provides estimated road geometry, distance, duration, and route steps. Leaflet displays the route on OpenStreetMap tiles. Use the external directions link for navigation; RouteCost itself does not provide live GPS navigation or traffic-aware ETAs.

Nearby hotels and guest houses are discovered from OpenStreetMap data within 5 km of the selected route stop. Their listing links open a Booking.com search prefilled with the stop, dates, and traveller count. To fetch and show live prices in RouteCost, configure `BOOKING_DEMAND_API_TOKEN` and `BOOKING_AFFILIATE_ID` in `.env`. Booking.com Demand API access requires managed-affiliate approval and credentials; without these settings, no price is fabricated. Search prices are cached for ten minutes because availability changes.

The route provider can flag some toll roads, but does not return current toll booth charges. Toll prices depend on route, vehicle class, and changing official schedules; add a verified amount as a Tolls expense. Toll charges are excluded until entered. Fuel price, mileage, and other expenses are user supplied estimates.

OpenStreetMap public services are community services with fair-use limits, not a production SLA. Attribution is shown in the map. If usage grows, configure a commercial/self-hosted geocoder, routing engine, tile service, and accommodation provider.

## Features

- Responsive dashboard and trip planner, using PKR by default.
- Multiple ordered stops and an interactive road route map.
- Estimated route distance, drive time, turn-step summary, and external turn-by-turn link.
- Vehicle and fuel inputs, other expenses including tolls, and cost-per-person calculation.
- Search, edit, and delete persisted trips.
- Server-side validation and parameterized SQLite queries.

## API

- `GET /api/health`
- `POST /api/routes/calculate` with `{ "start": "Lahore, Pakistan", "stops": ["Islamabad, Pakistan"] }`
- `POST /api/hotels/search` with a selected route stop's `lat`, `lon`, and `label`
- `POST /api/costs/calculate`
- `GET|POST /api/trips`
- `GET|PUT|DELETE /api/trips/<id>`

## Run tests

```powershell
python -m unittest discover -s tests -v
```

## Configuration

`.env.example` lists optional settings. `FLASK_DEBUG=1` is only for local development. `BOOKING_DEMAND_API_TOKEN` and `BOOKING_AFFILIATE_ID` enable live search prices when a managed affiliate account has access. Without them, stay discovery and Booking.com deep links remain available; the Booking.com API path is not enabled.
