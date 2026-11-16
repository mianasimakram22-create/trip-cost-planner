import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as routecost


def sample_trip(**overrides):
    trip = {
        "trip_name": "Northern loop",
        "start_location": "Lahore, Pakistan",
        "destinations": ["Islamabad, Pakistan", "Murree, Pakistan"],
        "distance": 500,
        "duration": 420,
        "vehicle_type": "Car",
        "fuel_average": 10,
        "fuel_price": 300,
        "travellers": 2,
        "currency": "PKR",
        "expenses": [{"category": "Tolls", "description": "Motorway", "amount": 500}],
    }
    trip.update(overrides)
    return trip


class RouteCostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(routecost, "DB_PATH", Path(self.temp.name) / "test.db")
        self.db_patch.start()
        routecost.init_db()
        routecost.app.config.update(TESTING=True)
        self.client = routecost.app.test_client()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_cost_calculation_includes_fuel_and_other_expenses(self):
        response = self.client.post("/api/costs/calculate", json=sample_trip())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["fuel_litres"], 50)
        self.assertEqual(response.json["fuel_cost"], 15000)
        self.assertEqual(response.json["total_cost"], 15500)
        self.assertEqual(response.json["per_person"], 7750)

    def test_invalid_mileage_and_negative_expense_are_rejected(self):
        zero_mileage = self.client.post("/api/costs/calculate", json=sample_trip(fuel_average=0))
        negative_expense = self.client.post("/api/costs/calculate", json=sample_trip(expenses=[{"category": "Food", "amount": -1}]))
        self.assertEqual(zero_mileage.status_code, 400)
        self.assertEqual(negative_expense.status_code, 400)

    def test_trip_create_retrieve_update_delete_and_persistence(self):
        created = self.client.post("/api/trips", json=sample_trip())
        self.assertEqual(created.status_code, 201)
        trip_id = created.json["id"]
        self.assertEqual(self.client.get("/api/trips").json[0]["trip_name"], "Northern loop")
        self.assertEqual(self.client.get(f"/api/trips/{trip_id}").json["destinations"][1], "Murree, Pakistan")
        changed = sample_trip(trip_name="Updated loop")
        self.assertEqual(self.client.put(f"/api/trips/{trip_id}", json=changed).json["trip_name"], "Updated loop")
        self.assertEqual(self.client.get(f"/api/trips/{trip_id}").json["trip_name"], "Updated loop")
        self.assertEqual(self.client.delete(f"/api/trips/{trip_id}").status_code, 200)
        self.assertEqual(self.client.get("/api/trips").json, [])

    def test_malformed_route_and_hotel_requests_are_rejected(self):
        self.assertEqual(self.client.post("/api/routes/calculate", json={"start": "", "stops": []}).status_code, 400)
        self.assertEqual(self.client.post("/api/hotels/search", json={}).status_code, 400)

    def test_route_response_contains_real_shape_without_external_calls(self):
        points = [{"lat": 31.5, "lon": 74.3, "label": "Lahore", "query": "Lahore"}, {"lat": 33.7, "lon": 73.1, "label": "Islamabad", "query": "Islamabad"}]
        upstream = {"code": "Ok", "routes": [{"distance": 120000, "duration": 7200, "geometry": {"type": "LineString", "coordinates": [[74.3, 31.5], [73.1, 33.7]]}, "legs": [{"steps": [{"name": "M-2", "distance": 120000, "maneuver": {"type": "depart", "modifier": "straight"}, "intersections": []}]}]}]}
        with patch.object(routecost, "geocode", side_effect=points), patch.object(routecost, "external_json", return_value=upstream):
            response = self.client.post("/api/routes/calculate", json={"start": "Lahore", "stops": ["Islamabad"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["distance_km"], 120)
        self.assertEqual(response.json["geometry"]["type"], "LineString")
        self.assertTrue(response.json["steps"])


if __name__ == "__main__":
    unittest.main()
