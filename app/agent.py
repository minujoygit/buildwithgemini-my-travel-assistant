# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import json
import os
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager
from google import genai
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.code_executors import AgentEngineSandboxCodeExecutor
from google.adk.memory import VertexAiMemoryBankService
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.cloud import firestore, storage
from google.genai import types

from .a2ui_utils import a2ui_callback

# IMPORTANT: Hardcode project ID and media bucket as string constants.
FIRESTORE_PROJECT = "qwiklabs-gcp-01-ffeca372cea5"
MEDIA_BUCKET = "my-travel-assistant-media-qwiklabs-gcp-01-ffeca372cea5"
MEMORY_BANK_ID = "5873483363439869952"


async def generate_memories_callback(callback_context: CallbackContext):
    try:
        await callback_context.add_session_to_memory()
    except Exception:
        try:
            mem_service = memory_service_builder()
            await mem_service.add_session_to_memory(session=callback_context.session)
        except Exception as inner_e:
            import logging
            logging.warning("Could not add session to memory bank: %s", inner_e)
    return None


def memory_service_builder():
    return VertexAiMemoryBankService(
        project=FIRESTORE_PROJECT,
        location="us-east1",
        agent_engine_id=MEMORY_BANK_ID,
    )


def _get_firestore_db():
    return firestore.Client(project=FIRESTORE_PROJECT)


def _get_sandbox_code_executor():
    metadata_file = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "deployment_metadata.json"
    )
    sandbox_resource_name = None
    agent_engine_resource_name = None

    if os.path.exists(metadata_file):
        try:
            with open(metadata_file, "r") as f:
                data = json.load(f)
            sandbox_resource_name = data.get("sandbox_resource_name")
            agent_engine_resource_name = data.get("remote_agent_runtime_id")
        except Exception:
            pass

    if sandbox_resource_name:
        return AgentEngineSandboxCodeExecutor(
            sandbox_resource_name=sandbox_resource_name
        )
    elif agent_engine_resource_name:
        return AgentEngineSandboxCodeExecutor(
            agent_engine_resource_name=agent_engine_resource_name
        )
    else:
        return AgentEngineSandboxCodeExecutor()


def explore_itinerary_route(
    origin_location: str,
    destination_location: str,
    intermediate_stops: str = "",
    travel_mode: str = "DRIVE",
) -> str:
    """Calculates distances, durations, and leg-by-leg routing for a multi-stop itinerary using Google Routes API.

    Args:
        origin_location: Starting location address or landmark (e.g. 'Fushimi Inari Taisha, Kyoto, Japan').
        destination_location: Final destination address or landmark (e.g. 'Kiyomizu-dera, Kyoto, Japan').
        intermediate_stops: Optional comma-separated list of stopover locations (e.g. 'Gion Corner, Kyoto, Japan').
        travel_mode: Travel mode ('DRIVE', 'WALK', 'BICYCLE', 'TRANSIT'). Default: 'DRIVE'.

    Returns:
        A JSON string containing total distance, total duration, and leg-by-leg routing breakdown.
    """
    try:
        api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")
        if not api_key:
            return "Error: GOOGLE_MAPS_API_KEY environment variable is not set."

        mode_clean = travel_mode.upper().strip()
        if mode_clean not in ["DRIVE", "WALK", "BICYCLE", "TRANSIT"]:
            mode_clean = "DRIVE"

        url = "https://routes.googleapis.com/directions/v2:computeRoutes"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": (
                "routes.distanceMeters,routes.duration,"
                "routes.legs.distanceMeters,routes.legs.duration,"
                "routes.legs.startLocation,routes.legs.endLocation"
            ),
        }

        payload = {
            "origin": {"address": origin_location},
            "destination": {"address": destination_location},
            "travelMode": mode_clean,
            "languageCode": "en-US",
            "units": "METRIC",
        }

        stops_list = []
        if intermediate_stops:
            if ";" in intermediate_stops:
                stops_list = [s.strip() for s in intermediate_stops.split(";") if s.strip()]
            elif "|" in intermediate_stops:
                stops_list = [s.strip() for s in intermediate_stops.split("|") if s.strip()]
            else:
                stops_list = [intermediate_stops.strip()] if intermediate_stops.strip() else []

            if stops_list:
                payload["intermediates"] = [{"address": s} for s in stops_list]

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        routes = data.get("routes", [])
        if not routes:
            return "No route found for the given itinerary."

        primary_route = routes[0]
        total_distance_m = primary_route.get("distanceMeters", 0)
        total_duration_sec_str = primary_route.get("duration", "0s").replace("s", "")
        try:
            total_duration_min = round(float(total_duration_sec_str) / 60.0, 1)
        except ValueError:
            total_duration_min = 0.0

        total_distance_km = round(total_distance_m / 1000.0, 2)

        legs_summary = []
        raw_legs = primary_route.get("legs", [])
        
        waypoints_labels = [origin_location]
        waypoints_labels.extend(stops_list)
        waypoints_labels.append(destination_location)

        for i, leg in enumerate(raw_legs):
            leg_dist_m = leg.get("distanceMeters", 0)
            leg_dur_str = leg.get("duration", "0s").replace("s", "")
            try:
                leg_dur_min = round(float(leg_dur_str) / 60.0, 1)
            except ValueError:
                leg_dur_min = 0.0

            from_label = waypoints_labels[i] if i < len(waypoints_labels) else f"Stop {i}"
            to_label = waypoints_labels[i+1] if (i + 1) < len(waypoints_labels) else f"Stop {i+1}"

            legs_summary.append({
                "leg_number": i + 1,
                "from": from_label,
                "to": to_label,
                "distance_km": round(leg_dist_m / 1000.0, 2),
                "duration_minutes": leg_dur_min,
            })

        # Generate interactive Google Maps Directions URL
        maps_base_url = "https://www.google.com/maps/dir/?api=1"
        maps_params = f"&origin={urllib.parse.quote(origin_location)}&destination={urllib.parse.quote(destination_location)}"
        if stops_list:
            waypoints_str = "|".join(stops_list)
            maps_params += f"&waypoints={urllib.parse.quote(waypoints_str)}"
        maps_params += f"&travelmode={mode_clean.lower()}"
        google_maps_url = maps_base_url + maps_params

        summary = {
            "travel_mode": mode_clean,
            "origin": origin_location,
            "destination": destination_location,
            "intermediate_stops": intermediate_stops,
            "total_distance_km": total_distance_km,
            "total_duration_minutes": total_duration_min,
            "google_maps_directions_url": google_maps_url,
            "route_legs": legs_summary,
            "source_api": "Google Routes API (computeRoutes)",
        }

        return json.dumps(summary, indent=2)
    except Exception as e:
        return f"Error computing route with Routes API: {e}"


def geocode_address(address: str) -> str:
    """Converts a street address, city, or landmark name into geographic coordinates using the Google Geocoding API.

    Args:
        address: The address or place name to geocode (e.g., 'Fushimi Inari Taisha, Kyoto', 'Eiffel Tower, Paris').

    Returns:
        A JSON string containing the formatted address, latitude, and longitude.
    """
    try:
        api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")
        if not api_key:
            return "Error: GOOGLE_MAPS_API_KEY environment variable is not set."

        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={urllib.parse.quote(address)}&key={api_key}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("status") != "OK" or not data.get("results"):
            return f"Geocoding failed with status: {data.get('status', 'NO_RESULTS')}"

        first_match = data["results"][0]
        location = first_match["geometry"]["location"]
        result = {
            "query": address,
            "formatted_address": first_match.get("formatted_address"),
            "latitude": location.get("lat"),
            "longitude": location.get("lng"),
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error executing Geocoding API: {e}"


def search_nearby_places(
    latitude: float,
    longitude: float,
    place_type: str = "tourist_attraction",
    radius_meters: float = 1000.0,
) -> str:
    """Finds nearby places of a specific type (e.g., 'tourist_attraction', 'restaurant', 'cafe', 'museum') around coordinates using Google Places API (New).

    Args:
        latitude: The center latitude coordinate (e.g., 34.96769).
        longitude: The center longitude coordinate (e.g., 135.77918).
        place_type: Type of place to search for (e.g. 'tourist_attraction', 'restaurant', 'cafe', 'museum', 'hotel').
        radius_meters: Search radius in meters (default 1000.0).

    Returns:
        A JSON string containing a list of nearby places with name, formatted address, and location coordinates.
    """
    try:
        api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")
        if not api_key:
            return "Error: GOOGLE_MAPS_API_KEY environment variable is not set."

        url = "https://places.googleapis.com/v1/places:searchNearby"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location",
        }
        payload = {
            "includedTypes": [place_type.strip().lower()],
            "maxResultCount": 10,
            "locationRestriction": {
                "circle": {
                    "center": {
                        "latitude": float(latitude),
                        "longitude": float(longitude),
                    },
                    "radius": float(radius_meters),
                }
            },
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        raw_places = data.get("places", [])
        if not raw_places:
            return f"No nearby places found for type '{place_type}' within {radius_meters}m."

        results = []
        for p in raw_places:
            display_name = p.get("displayName", {})
            name_text = (
                display_name.get("text", "Unknown Name")
                if isinstance(display_name, dict)
                else str(display_name)
            )
            loc = p.get("location", {})
            results.append(
                {
                    "name": name_text,
                    "address": p.get("formattedAddress"),
                    "location": {
                        "latitude": loc.get("latitude"),
                        "longitude": loc.get("longitude"),
                    },
                }
            )

        return json.dumps({"places": results}, indent=2)
    except Exception as e:
        return f"Error executing Places API (New) searchNearby: {e}"


def search_destinations(category: str = "", max_daily_budget_usd: float = 0.0) -> str:
    """Searches the travel destination database in Firestore.

    Args:
        category: Optional category filter (e.g. 'Culture', 'City', 'Beach', 'Nature').
        max_daily_budget_usd: Optional upper limit for daily estimated cost in USD.

    Returns:
        A JSON string containing a list of matching travel destination documents.
    """
    try:
        db = _get_firestore_db()
        docs = db.collection("destinations").stream()
        results = []
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            
            # Apply filters if provided
            if category and data.get("category", "").lower() != category.lower():
                continue
            if max_daily_budget_usd > 0 and data.get("estimated_cost_per_day_usd", 0) > max_daily_budget_usd:
                continue

            results.append(data)

        if not results:
            return "No matching destinations found in the database."
        return json.dumps(results, indent=2)
    except Exception as e:
        return f"Error querying Firestore database: {e}"


def calculate_trip_budget(
    destination_name: str,
    number_of_days: int,
    number_of_travelers: int = 1,
    travel_tier: str = "Mid-range",
) -> str:
    """Calculates an itemized trip budget breakdown for a destination.

    Args:
        destination_name: Name or ID of the destination (e.g., 'Kyoto', 'Paris', 'Barcelona').
        number_of_days: Total duration of the trip in days.
        number_of_travelers: Number of travelers (default: 1).
        travel_tier: Travel style tier ('Budget', 'Mid-range', or 'Luxury').

    Returns:
        A JSON string containing the itemized daily and total trip budget breakdown.
    """
    try:
        db = _get_firestore_db()
        daily_rate_usd = 150.0  # default fallback
        found_name = destination_name

        # Look up destination in Firestore to retrieve accurate daily cost
        doc_id = destination_name.lower().strip().replace(" ", "_")
        doc_ref = db.collection("destinations").document(doc_id)
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            daily_rate_usd = float(data.get("estimated_cost_per_day_usd", 150.0))
            found_name = data.get("name", destination_name)
        else:
            # Try case-insensitive search
            docs = db.collection("destinations").stream()
            for d in docs:
                data = d.to_dict()
                if data.get("name", "").lower() == destination_name.lower().strip():
                    daily_rate_usd = float(data.get("estimated_cost_per_day_usd", 150.0))
                    found_name = data.get("name", destination_name)
                    break

        # Tier multiplier adjustment
        tier_clean = travel_tier.lower().strip()
        if "budget" in tier_clean:
            multiplier = 0.7
            tier_label = "Budget"
        elif "luxury" in tier_clean:
            multiplier = 2.0
            tier_label = "Luxury"
        else:
            multiplier = 1.0
            tier_label = "Mid-range"

        adjusted_daily_per_person = daily_rate_usd * multiplier
        
        # Breakdown estimated proportions per day per person
        accommodation_per_day = round(adjusted_daily_per_person * 0.50, 2)
        meals_per_day = round(adjusted_daily_per_person * 0.30, 2)
        activities_per_day = round(adjusted_daily_per_person * 0.20, 2)

        total_per_person = round(adjusted_daily_per_person * number_of_days, 2)
        base_total = round(total_per_person * number_of_travelers, 2)
        contingency_buffer = round(base_total * 0.10, 2)
        grand_total = round(base_total + contingency_buffer, 2)

        budget_summary = {
            "destination": found_name,
            "number_of_days": number_of_days,
            "number_of_travelers": number_of_travelers,
            "travel_tier": tier_label,
            "daily_cost_breakdown_per_person_usd": {
                "accommodation": accommodation_per_day,
                "meals_and_dining": meals_per_day,
                "activities_and_transport": activities_per_day,
                "total_daily_per_person": round(adjusted_daily_per_person, 2),
            },
            "total_per_person_usd": total_per_person,
            "base_trip_total_usd": base_total,
            "contingency_buffer_usd": contingency_buffer,
            "grand_total_usd": grand_total,
        }

        return json.dumps(budget_summary, indent=2)
    except Exception as e:
        return f"Error calculating trip budget: {e}"


async def generate_destination_image(
    destination_name: str,
    tool_context: ToolContext,
    prompt_description: str = "",
) -> str:
    """Generates a scenic image for a travel destination and saves it BOTH as an ADK Artifact and to Cloud Storage with a meaningful filename.

    Args:
        destination_name: Name of the travel spot or destination (e.g., 'Kyoto', 'Eiffel Tower', 'Mount Fuji').
        prompt_description: Optional visual description or style for the image (e.g., 'vibrant sunset with cherry blossoms').

    Returns:
        A JSON string containing the destination name, filename, public Cloud Storage image URL, and artifact status.
    """
    try:
        client = genai.Client(
            vertexai=True,
            project=FIRESTORE_PROJECT,
            location="global",
        )

        prompt = f"A high-quality scenic image of {destination_name}."
        if prompt_description:
            prompt += f" {prompt_description}"

        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-image",
            contents=prompt,
        )

        if not response.candidates or not response.candidates[0].content.parts:
            return f"Error: No response generated for '{destination_name}'."

        first_part = response.candidates[0].content.parts[0]
        if not first_part.inline_data or not first_part.inline_data.data:
            return f"Error: No image bytes returned for '{destination_name}'."

        image_bytes = first_part.inline_data.data
        mime_type = first_part.inline_data.mime_type or "image/png"
        ext = "jpg" if "jpeg" in mime_type.lower() else "png"

        # Meaningful filename based on destination name
        clean_name = "".join(c for c in destination_name.lower().strip().replace(" ", "_") if c.isalnum() or c == "_")
        filename = f"{clean_name}.{ext}"

        # 1. Save artifact so it shows up in Playground's Artifacts panel
        await tool_context.save_artifact(
            filename=filename,
            artifact=types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
        )

        # 2. Upload image bytes directly to Cloud Storage
        storage_client = storage.Client(project=FIRESTORE_PROJECT)
        bucket = storage_client.bucket(MEDIA_BUCKET)
        blob = bucket.blob(filename)
        blob.upload_from_string(image_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{MEDIA_BUCKET}/{filename}"

        result = {
            "destination": destination_name,
            "filename": filename,
            "public_image_url": public_url,
            "artifact_saved": True,
            "status": "Image successfully generated, saved to Playground Artifacts, and published to Cloud Storage.",
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error generating destination image: {e}"


def get_currency_exchange_rates(
    base_currency: str = "USD", target_currencies: str = "EUR,JPY,GBP,CAD,AUD"
) -> str:
    """Fetches live real-world currency exchange rates for travel budget planning using the public Frankfurter API.

    Args:
        base_currency: The 3-letter source currency code (e.g., 'USD', 'EUR', 'GBP').
        target_currencies: Comma-separated list of target currency codes (e.g., 'EUR,JPY,GBP,CAD,AUD').

    Returns:
        A JSON string containing current conversion rates and date.
    """
    try:
        api_key = os.getenv("EXCHANGE_RATE_API_KEY", "")
        base = base_currency.upper().strip()
        targets = ",".join(
            [c.strip().upper() for c in target_currencies.split(",") if c.strip()]
        )

        url = f"https://api.frankfurter.app/latest?from={urllib.parse.quote(base)}&to={urllib.parse.quote(targets)}"
        headers = {"User-Agent": "MyTravelAssistant/1.0"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))

        result = {
            "base_currency": data.get("base", base),
            "date": data.get("date"),
            "rates": data.get("rates", {}),
            "source_api": "Frankfurter Public API (https://api.frankfurter.app)",
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error fetching exchange rates from public API: {e}"


def google_search_web(query: str) -> str:
    """Searches Google for live real-world information, travel news, attractions, and local events.

    Args:
        query: The search query string (e.g., 'top attractions in Kyoto', 'Paris travel events').

    Returns:
        Up-to-date live search results grounded by Google Search.
    """
    try:
        client = genai.Client(
            vertexai=True,
            project=FIRESTORE_PROJECT,
            location="us-central1",
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=query,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
        return response.text or "No search results returned."
    except Exception as e:
        return f"Error executing Google Search: {e}"


def add_destination(
    name: str,
    country: str,
    category: str,
    description: str,
    estimated_cost_per_day_usd: float,
    best_season: str,
    highlights: str,
) -> str:
    """Adds a new destination record to the Firestore database.

    Args:
        name: Name of the destination city or spot (e.g., 'Kyoto').
        country: Country where the destination is located.
        category: Category of travel ('Culture', 'City', 'Beach', 'Nature').
        description: Brief description of the destination.
        estimated_cost_per_day_usd: Estimated daily budget in USD.
        best_season: Ideal season to visit (e.g., 'Spring / Autumn').
        highlights: Comma-separated key highlights or top attractions.

    Returns:
        A status message confirming insertion into Firestore.
    """
    try:
        db = _get_firestore_db()
        doc_id = name.lower().replace(" ", "_")
        highlight_list = [h.strip() for h in highlights.split(",") if h.strip()]
        
        doc_data = {
            "name": name,
            "country": country,
            "category": category,
            "description": description,
            "estimated_cost_per_day_usd": float(estimated_cost_per_day_usd),
            "best_season": best_season,
            "highlights": highlight_list,
        }
        
        db.collection("destinations").document(doc_id).set(doc_data)
        return f"Successfully added destination '{name}' ({doc_id}) to Firestore!"
    except Exception as e:
        return f"Error adding destination to Firestore: {e}"


def get_weather(query: str) -> str:
    """Simulates weather search for a destination.

    Args:
        query: Location query string.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        return "It's 60 degrees and foggy."
    elif "kyoto" in query.lower():
        return "It's 68 degrees and pleasant."
    elif "paris" in query.lower():
        return "It's 62 degrees with light rain."
    elif "maui" in query.lower():
        return "It's 82 degrees and sunny."
    elif "reykjavik" in query.lower():
        return "It's 45 degrees and breezy."
    return "It's 75 degrees and clear."


def get_current_time(query: str) -> str:
    """Simulates getting the current time for a city.

    Args:
        query: The name of the city.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        tz_identifier = "America/Los_Angeles"
    elif "kyoto" in query.lower() or "japan" in query.lower():
        tz_identifier = "Asia/Tokyo"
    elif "paris" in query.lower() or "france" in query.lower():
        tz_identifier = "Europe/Paris"
    else:
        return f"Current time for query {query}: UTC {datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')}"

    tz = ZoneInfo(tz_identifier)
    now = datetime.datetime.now(tz)
    return f"The current time in {query} is {now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}"


schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

a2ui_instruction = schema_manager.generate_system_prompt(
    role_description=(
        "You are My Travel Assistant, a helpful AI concierge. "
        "You actively remember all user info (e.g., name, home location, contact details, personal background) and user travel preferences (e.g., budget constraints, dietary restrictions, preferred airlines/hotels, activity interests, travel style, and travel habits) across sessions. "
        "Always personalize your recommendations, itineraries, and responses using the remembered user info and preferences. "
        "You can compute multi-stop itinerary routes using the Google Routes API, "
        "geocode addresses into coordinates using the Geocoding API, search for nearby places "
        "using Google Places API (New), search travel destinations from Firestore, calculate itemized trip budgets, "
        "convert currencies, search live web information using Google Search, generate scenic concept art images "
        "using gemini-3.1-flash-lite-image, add new travel spots to Firestore, check weather forecasts, and retrieve current local times. "
        "When requested to calculate trip costs or perform custom mathematical analysis, write Python code inside ```python ... ``` blocks to run it in the Python sandbox. "
        "Whenever you call explore_itinerary_route, always provide the google_maps_directions_url as a clickable Markdown link. "
        "Whenever you call generate_destination_image, display the generated public image using Markdown ![Destination Image](public_image_url)."
    ),
    workflow_description="Analyze the request and return structured UI when appropriate.",
    ui_description=(
        "Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. "
        "Never nest a Card inside a Card. "
        "Use ONLY these components: Card, Column, Row, Text, and Image. Do not use "
        "Table or Heading (unsupported), or Buttons, actions, or forms (they do "
        "nothing in adk web). "
        "You may include one Image component, but only when you have a public https "
        "URL for the image (for example the URL an image tool returns after uploading "
        "to a public bucket). Set the Image url to that exact https link, for example "
        "{\"Image\": {\"url\": {\"literalString\": \"https://...\"}}}. Never point an "
        "Image at a bare filename, an artifact name, or a non-http(s) path. If you do "
        "not have a public URL, add a short Text line noting the image instead. "
        "No markdown in text; use the usageHint property ('h1', 'h2', 'body') for "
        "headings and emphasis. "
        "Output ONLY the raw A2UI JSON array — no prose, and never wrap it in "
        "<a2a_datapart_json> tags or 'kind'/'data'/'metadata' objects."
    ),
    include_schema=True,
    include_examples=True,
)

root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-2.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    code_executor=_get_sandbox_code_executor(),
    instruction=a2ui_instruction,
    tools=[
        PreloadMemoryTool(),
        explore_itinerary_route,
        geocode_address,
        search_nearby_places,
        search_destinations,
        calculate_trip_budget,
        get_currency_exchange_rates,
        google_search_web,
        generate_destination_image,
        add_destination,
        get_weather,
        get_current_time,
    ],
    after_model_callback=a2ui_callback,
    after_agent_callback=generate_memories_callback,
)

app = App(
    root_agent=root_agent,
    name="app",
)
