# Saved Trip Schema

Use UTF-8 JSON. Save only after explicit user confirmation.

## Required top-level fields

```json
{
  "id": "stable-trip-id",
  "destination": {
    "city": "<user-confirmed>",
    "country": "<known-or-user-confirmed>"
  },
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "duration_days": 1,
  "travelers": 1,
  "arrival_departure": {
    "origin_or_current_location": "<user-confirmed>",
    "arrival": "<user-confirmed>",
    "departure": "<user-confirmed>"
  },
  "budget": {
    "total": 0,
    "currency": "CNY",
    "includes_intercity_transport": false,
    "categories": {
      "accommodation": 0,
      "food": 0,
      "local_transport": 0,
      "activities": 0,
      "intercity_transport": 0,
      "shopping": 0,
      "contingency": 0
    }
  },
  "accommodation": {
    "nights": 0,
    "preference": "<user-confirmed-or-empty>"
  },
  "confirmed_fields": [
    "destination",
    "dates",
    "travelers",
    "arrival_departure",
    "budget"
  ],
  "assumptions": [],
  "itinerary": [
    {
      "day": 1,
      "date": "YYYY-MM-DD",
      "title": "Day theme",
      "activities": [
        {
          "time": "09:00",
          "name": "Activity",
          "location": "Location",
          "estimated_cost": 0,
          "verification": "web_verified"
        }
      ]
    }
  ],
  "sources": [
    {
      "type": "official_web",
      "title": "Source title",
      "url": "https://example.com",
      "verified_at": "YYYY-MM-DD"
    }
  ]
}
```

## Rules

- Set `duration_days` to the inclusive date count.
- Set `accommodation.nights` to the date difference.
- Include one itinerary entry for every trip date.
- Use an empty string or `null` for optional unknown values; never fabricate them.
- Keep critical `assumptions` empty in save mode.
- Add a field to `confirmed_fields` only when the user explicitly confirmed it.
- Make budget category values non-negative and ensure their sum does not exceed `budget.total`.
- Use `verification` values: `user_confirmed`, `local_source`, `web_verified`, `recommendation`, or `unverified`.

