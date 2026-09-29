from google.cloud import firestore

FIRESTORE_PROJECT = "qwiklabs-gcp-01-ffeca372cea5"

SEED_DESTINATIONS = [
    {
        "id": "kyoto",
        "name": "Kyoto",
        "country": "Japan",
        "category": "Culture",
        "description": "Historical heart of Japan with serene temples, traditional tea houses, and bamboo groves.",
        "estimated_cost_per_day_usd": 150.0,
        "best_season": "Spring / Autumn",
        "highlights": ["Fushimi Inari Shrine", "Arashiyama Bamboo Grove", "Gion District"],
    },
    {
        "id": "paris",
        "name": "Paris",
        "country": "France",
        "category": "City",
        "description": "City of Light known for art, world-class cuisine, fashion, and iconic architecture.",
        "estimated_cost_per_day_usd": 220.0,
        "best_season": "Spring / Early Autumn",
        "highlights": ["Eiffel Tower", "Louvre Museum", "Montmartre Walking Tour"],
    },
    {
        "id": "maui",
        "name": "Maui",
        "country": "USA",
        "category": "Beach",
        "description": "Tropical paradise featuring volcanic landscapes, pristine beaches, and whale watching.",
        "estimated_cost_per_day_usd": 300.0,
        "best_season": "Winter / Spring",
        "highlights": ["Road to Hana", "Haleakala Crater", "Kaanapali Beach"],
    },
    {
        "id": "reykjavik",
        "name": "Reykjavik",
        "country": "Iceland",
        "category": "Nature",
        "description": "Gateway to geothermal wonders, Northern Lights, glaciers, and dramatic waterfalls.",
        "estimated_cost_per_day_usd": 250.0,
        "best_season": "Winter (Northern Lights) / Summer (Midnight Sun)",
        "highlights": ["Blue Lagoon", "Golden Circle Tour", "Hallgrímskirkja"],
    },
]


def seed_database():
    """Seeds the Firestore database with initial destination records."""
    db = firestore.Client(project=FIRESTORE_PROJECT)
    collection = db.collection("destinations")
    print(f"Connecting to Firestore project: {FIRESTORE_PROJECT}")
    for item in SEED_DESTINATIONS:
        doc_id = item["id"]
        doc_data = {k: v for k, v in item.items() if k != "id"}
        collection.document(doc_id).set(doc_data)
        print(f"✅ Seeded destination: {item['name']} ({doc_id})")


if __name__ == "__main__":
    seed_database()
