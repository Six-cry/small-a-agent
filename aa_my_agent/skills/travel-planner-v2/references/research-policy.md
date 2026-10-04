# Travel Research Policy

Use this policy for current facts or research that combines local and web sources.

## Source order

1. Search local knowledge for destination guides, user-supplied PDFs, stable cultural background, and route ideas.
2. Query the weather tool for its supported forecast horizon.
3. Search the web for time-sensitive facts.
4. Fetch the selected pages before treating snippets as verified.

Prefer sources in this order:

1. government and official tourism authorities;
2. venue and transport-operator websites;
3. established booking platforms for availability estimates;
4. reputable editorial guides;
5. personal posts only for subjective ideas.

## Evidence labels

Classify material internally as:

- `user_confirmed`: explicitly stated or confirmed by the user;
- `local_source`: retrieved from the local knowledge base;
- `web_verified`: supported by a fetched current page;
- `recommendation`: a planning judgment based on evidence;
- `assumption`: provisional and clearly shown to the user;
- `unverified`: not safe to present as a current fact.

Do not turn `recommendation`, `assumption`, or `unverified` content into a confirmed user field.

## Freshness

- Use the actual current date and year in searches.
- Check the publication or effective date when available.
- Verify prices, opening hours, event dates, service schedules, booking rules, and safety notices against current pages.
- Do not use an older year's event schedule for the current year.
- A fetched official page can still be stale; state the date when material.

## Tool failures

Treat any output beginning with `Error:` or reporting no results as a failed research step. Retry with a narrower query or a different official source. If verification still fails, state that the fact is unverified.

## Conflicts

When sources disagree:

1. prefer the more authoritative and current source;
2. describe the conflict briefly;
3. avoid a false single answer;
4. advise the user to confirm directly when the decision is consequential.

