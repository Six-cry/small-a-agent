# Travel Plan Output Format

Use the smallest format that satisfies the request.

## Start with plan status

State:

- destination and dates;
- draft or confirmed-plan mode;
- confirmed traveler count and budget, if known;
- cost scope, such as whether intercity transport is included;
- any assumptions or unresolved blocking details.

## Detailed itinerary

For each day include:

- date and theme;
- morning, afternoon, and evening blocks;
- realistic travel and rest buffers;
- meal area or cuisine suggestion;
- estimated local cost with scope;
- booking or weather alternative when relevant.

Do not invent an arrival airport, station, or time.

## Budget

Separate:

- transport to and from the destination;
- accommodation;
- local transportation;
- meals;
- tickets and activities;
- shopping;
- contingency.

Show category totals and the resulting grand total. If the user has not provided a budget, present an estimate range labeled as a draft rather than choosing a budget for them.

## Sources

Provide:

- local filename and page for local RAG material;
- direct URLs for verified current facts;
- a short `Unverified` section for unresolved prices or schedules.

## Closing

Ask for only the next decision that materially changes the plan. Do not claim the plan was saved unless it was validated, stored, and read back.

