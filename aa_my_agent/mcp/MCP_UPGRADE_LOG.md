# MCP Upgrade Log

This log stays with the MCP subsystem. It distinguishes production integration
from isolated experiments and records observed results rather than command exit
codes alone.

## 2026-09-29 — Generic MCP client layer

- **Work type:** Integrated into the production Agent pipeline. The bundled
  `travel-demo` server is a local verification fixture, not a live travel-data
  provider.
- **Problem:** Small-a could call only statically registered in-process tools.
  Adding maps, calendars, hotel search, or other external services would require
  one-off tool definitions and handlers for every provider.
- **Cause:** `agent.py` consumed the fixed `TOOLS` and `TOOL_HANDLERS` objects,
  and the permission hook rejected every tool outside that static registry.
- **Change:** Added a persistent MCP 2.x client runtime with stdio and Streamable
  HTTP transports, local JSON configuration, environment-variable token
  references, tool discovery, `mcp__server__tool` namespacing, schema adaptation,
  output limiting, untrusted-output labeling, and clean shutdown. The existing
  tool registry now merges discovered MCP tools dynamically. Exact-name overlaps
  with common built-ins are hidden by default. Only tools explicitly annotated
  read-only and non-destructive bypass confirmation; all other MCP tools require
  user approval. MCP tool calls are never automatically retried.
- **Verification method:** Ran Python syntax compilation; eight focused pytest
  cases for configuration validation, HTTPS enforcement, discovery, dispatch,
  permissions, built-in shadow suppression, and an official-SDK in-process
  round trip; launched `aa_my_agent.mcp_servers.travel_demo` as a real stdio
  subprocess and compared the discovered name, permission classification, and
  structured response with the server source; ran the complete `aa_my_agent/test`
  and `aa_my_agent/tests` suite.
- **Actual result:** The focused MCP suite passed 8/8. The real stdio check
  connected `travel-demo`, discovered
  `mcp__travel-demo__get_travel_mcp_status`, classified it read-only, returned
  the expected structured status, and shut down normally. The full small-a suite
  passed 73 tests and retained three previously known failures in
  `tests/test_time_tool.py`; those failures come from that test's fake config
  module omitting `RAG_ALLOW_ON_DEMAND_IMAGE_VERIFY` and are unrelated to MCP.
  Installing MCP 2.2.0 exposed an existing FastAPI/Starlette constraint conflict;
  FastAPI was upgraded from 0.110.2 to 0.141.1, after which that new conflict was
  removed. Other pre-existing environment conflicts reported by `pip check` were
  not changed.
- **Current status:** Generic MCP discovery and calls are available but opt-in.
  With MCP disabled or no configuration file present, the original tool set and
  Agent behavior remain unchanged. No live map, calendar, hotel, booking, or
  payment provider is configured yet.
- **Remaining work:** Select a travel provider, create
  `aa_my_agent/mcp/servers.json` from the example, add the provider endpoint and
  token environment variable, then perform representative travel queries against
  the provider's source data. Calendar writes, bookings, cancellations, and
  payments must retain confirmation and idempotency controls before production
  use.

## 2026-09-29 — AMap official MCP configuration

- **Work type:** Integrated provider configuration with discovery verification;
  live map-data evaluation remains pending because no user API Key is configured.
- **Problem:** The generic MCP layer did not yet supply real POI, coordinate,
  distance, or route data for domestic travel planning.
- **Cause:** No concrete map provider was configured, and the AMap official npm
  server does not currently advertise MCP read-only annotations, so the default
  safety policy would request approval for every query.
- **Change:** Added a production `servers.json` entry for the official
  `@amap/amap-maps-mcp-server`, launched through `npx.cmd` with the API Key passed
  only from `AMAP_MAPS_API_KEY`. The command uses the npm official registry only
  for this process and does not change the user's global mirror. Added a reviewed
  per-server `read_only_tools` override that cannot override an explicit
  destructive annotation. Enabled ten POI/geocoding/distance/routing tools and
  excluded AMap weather (duplicate of the built-in tool) and IP location.
- **Verification method:** Confirmed Node.js v24.16.0 satisfies AMap's documented
  v22.14.0 minimum; launched the official npm server with a discovery-only
  placeholder Key; compared the 12 discovered tools with AMap's documented
  capability set; loaded the committed configuration and verified that exactly
  ten intended tools were exposed and classified read-only; ran ten MCP-focused
  tests and the full small-a suite.
- **Actual result:** The official server started over stdio and the committed
  configuration exposed geocoding, reverse geocoding, text/nearby/detail search,
  distance, walking, cycling, driving, and transit routing. Weather and IP
  location were absent. All ten exposed tools were classified read-only. The MCP
  suite passed 10/10; the full suite passed 75 tests and retained the same three
  pre-existing fake-config failures in `tests/test_time_tool.py`. No live AMap
  query was made because `AMAP_MAPS_API_KEY` is not set.
- **Current status:** Code and provider configuration are ready. MCP remains
  disabled in the user's local `.env`, and no AMap Key is present, so small-a
  cannot yet retrieve live map data.
- **Remaining work:** The user must create a Web Service Key in the AMap console,
  set `MCP_ENABLED=true` and `AMAP_MAPS_API_KEY=...` in the local `.env`, then run
  representative POI and route queries and compare returned locations, distances,
  and durations with the AMap application or official web result.

## 2026-09-30 — Per-turn read deduplication and AMap route reduction

- **Work type:** Integrated into the production Agent and MCP result pipeline,
  using the user's recorded live AMap response as a representative evaluation
  fixture. This is not an isolated experiment.
- **Problem:** After L2/L4 context compaction, the model could issue the same
  read-only AMap route call again because completed tool-call state existed only
  inside compactable messages. A live transit result was 22,783 JSON characters,
  so each repeat also consumed enough context to trigger further compaction.
- **Cause:** S11 recovery protects model calls from output limits, context errors,
  and transient failures, but it intentionally had no tool-result identity cache
  or provider-specific result normalization. The generic MCP formatter only
  applied a final character limit and did not understand route structure.
- **Change:** Added a per-user-turn cache keyed by MCP tool name plus canonical
  JSON arguments. It accepts only tools explicitly classified read-only, lives
  outside the compacted message list, caches only successful results, and is
  discarded at turn end. Cache hits still return the retained result to satisfy
  the model's tool protocol but do not call the provider again. Added an AMap
  route reducer before model ingestion for transit, driving, walking, and cycling
  results above 6,000 characters. Transit reduction keeps the first three AMap
  options, totals, durations, walking distances, transit lines, boarding and
  alighting stops, via-stop names, entrances/exits, and representative transfer
  walking instructions while dropping geometry and verbose repetition. The full
  raw JSON is stored under ignored `storage/mcp_results/` using a content hash;
  no local path is exposed to the model. MCP error results are explicitly marked
  failed and never cached. Turn telemetry now reports `mcp_cache_hits`.
- **Verification method:** Added regression cases for canonical argument-order
  matching, exclusion of write and built-in tools, same-turn provider call
  suppression, failed-result non-caching, structural AMap reduction, raw-copy
  persistence, and polyline removal. Ran the complete MCP and Agent-loop tests,
  then the full small-a test suite. Separately replayed the live transit JSON
  captured on 2026-09-29 and compared the reduced result against the source for
  origin, destination, total distance, the first three option durations and
  walking distances, and their primary transit-line names.
- **Actual result:** MCP and Agent-loop tests passed 24/24. The full suite passed
  79 tests and retained the same three pre-existing failures in
  `tests/test_time_tool.py`, whose fake config omits
  `RAG_ALLOW_ON_DEMAND_IMAGE_VERIFY`. The live result was reduced from 22,783 to
  7,318 characters (67.9% smaller). All compared route facts matched exactly,
  three of five provider-ranked options remained, and the full raw copy was
  written successfully. Git ignore verification matched `storage/*` for that
  copy.
- **Current status:** Exact duplicate read-only MCP calls within one user turn no
  longer reach the provider, including after message compaction. Large supported
  AMap route responses are structurally reduced before the model sees them;
  small and non-route MCP responses retain the generic formatter behavior.
- **Remaining work:** Observe several real driving, walking, cycling, and
  cross-city transit requests to tune provider-specific fields if AMap returns
  additional shapes. If other MCP providers later produce large results, add
  separate reviewed reducers rather than applying AMap assumptions globally.

## 2026-09-30 — Weather, intercity search, rail handoff, and Google Calendar

- **Work type:** Integrated local MCP server implementations and production
  configuration. The live Open-Meteo call and local MCP discovery are verified;
  Duffel and Google account operations remain credential-gated, not live-verified.
- **Problem:** With only AMap, small-a could route within a city but could not
  query dated hourly weather, search flight offers or dated hotel availability,
  hand a train user to the official source, or place a confirmed itinerary on
  the user's Google Calendar.
- **Cause:** The generic MCP client had no concrete servers for these domains.
  The built-in weather tool provides only a short daily summary. AMap hotel POIs
  do not include dated room availability. No documented public 12306 developer
  API was identified for official live ticket lookup. The existing MCP HTTP
  transport does not implement Google's interactive OAuth and token refresh.
- **Change:** Added four local stdio MCP servers to `servers.json` and the example
  config. Open-Meteo provides city-based WGS84 geocoding and bounded hourly
  forecasts with source attribution. Duffel exposes flight offer and hotel
  availability searches, bounded to ten compact results with explicit test/live
  labels; no order, payment, cancellation or booking tools exist. The rail tool
  returns only official 12306 query URLs and explicitly marks `live_data=false`.
  The Google Calendar bridge supports upcoming event listing and event creation
  with refreshed OAuth access tokens. A separate opt-in loopback authorization
  helper obtains a refresh token and saves it to the ignored `.env` using an
  atomic replacement; on Windows it preserves the protected file ACL. Calendar
  creation is classified as a write and shows the event details before asking
  for approval. Optional Duffel/Google servers become active automatically
  when their listed environment variables are present; otherwise startup reports
  them as pending without trying invalid connections. Added `requests`, Windows
  `pywin32` and `tzdata` dependencies and a setup guide beside the MCP code.
- **Verification method:** Mocked provider responses were compared with the
  compact outputs for hourly weather, Duffel flight/hotel searches, and Google
  event listing/creation. Checked expected provider failures produce concise
  MCP errors rather than cached successful results. Started all four local
  servers together through the production MCP manager with temporary discovery
  credentials, checked their six discovered tools and read/write permissions,
  made a live Open-Meteo hourly query for Nanjing, ran the new focused tests,
  then ran the full `aa_my_agent/test` and `aa_my_agent/tests` suite.
- **Actual result:** All four new local servers connected in the discovery test.
  Exactly six tools were discovered; Google event creation was `write`, while
  listing was `read`. The live Nanjing hourly forecast returned four local-time
  rows for 09:00–12:00 with temperature, rain probability, precipitation and
  wind fields after switching from `urllib` (TLS EOF on this machine) to
  `requests` (HTTP 200). The merged MCP/provider tests passed 29/29, and the
  full suite passed 102 tests with the same three existing
  `tests/test_time_tool.py` fake-config failures. No Duffel live search or real
  Google authorization was attempted because the user has no credentials yet.
- **Current status:** Hourly weather and the honest 12306 official-link handoff
  are available whenever MCP is enabled. Duffel flight/hotel tools are ready
  but pending a Duffel token (and separate Stays access for hotels). Google
  Calendar tools are ready but pending OAuth client details and the user's
  one-time authorization. Existing AMap behavior is preserved.
- **Remaining work:** User obtains a Duffel test/live token and Stays permission,
  creates a Google Desktop OAuth client, runs the local authorization helper,
  and restarts small-a. After that, verify real provider-specific offers and
  calendar events against those providers' own interfaces. The rail handoff
  remains non-live until a documented authorized data source is available.

## 2026-10-01 — Replace the active Google Calendar connector with DingTalk

- **Work type:** Integrated production MCP connector and configuration. Provider
  behavior was tested with simulated DingTalk responses and a real local stdio
  MCP handshake; no account-side API call was made without user credentials.
- **Problem:** The user prefers DingTalk calendar for domestic travel planning
  and does not want to complete Google OAuth setup. The previous active calendar
  configuration could only expose Google Calendar tools.
- **Cause:** The MCP configuration and calendar bridge were specific to Google.
  DingTalk calendar uses organization app credentials and a target user's Union
  ID. The broad official npm MCP package also exposes unrelated profiles and
  writes its access-token cache into its installed package directory, so its
  default behavior is not an appropriate narrow bridge for this agent.
- **Change:** Added a local `dingtalk_calendar` MCP server exposing only bounded
  primary-calendar event lookup and creation. It reads Client ID, Client Secret,
  and target Union ID from the environment; obtains a DingTalk app token by
  HTTPS POST and caches it only in process memory. Results keep compact event
  fields, and provider errors do not echo tokens or secrets. Creation is marked
  as a write, so the existing MCP permission hook requests confirmation; the
  tool does not retry uncertain writes. Activated DingTalk behind these three
  environment variables in the production and example server configurations,
  disabled Google without deleting its implementation, and updated setup docs.
- **Verification method:** Compared mocked token, date-range query, and event
  creation requests and responses with the official DingTalk MCP calendar
  profile's endpoint/field definitions and the provider's documented event
  shapes. Checked timestamp validation, bounded query ranges, secret-free
  errors, read/write discovery, active configuration, and actual stdio process
  discovery. Ran the full small-a test suite before the final stdio-only test.
- **Actual result:** The full suite passed 134 tests after the connector and
  configuration change. The final focused suite, including stdio discovery,
  passed 6/6. The stdio server exposed exactly two tools; lookup was classified
  read-only and creation as write. No real DingTalk event was read or created.
- **Current status:** DingTalk is the selected calendar connector but remains
  pending until the user configures an organization internal app, the required
  calendar permissions, and their Union ID. Google is disabled in the active
  MCP configuration and can be re-enabled later if requested.
- **Remaining work:** After the user supplies credentials locally, verify one
  short-range lookup against the DingTalk calendar, then create a clearly
  approved test event and confirm it appears in the app. If the user lacks an
  organization/application administrator, this authorization path is not yet
  available; choose a different calendar provider or an import-only ICS path.
补充验证（2026-10-01）：最终全量回归 `python -m pytest -q aa_my_agent/test aa_my_agent/tests` 为 135 passed（此前记录的 134 项是在新增真实 MCP 子进程连接测试之前）。这是本地模拟与协议验证，不代表已用真实钉钉账号完成联调；待配置组织应用凭证与 Union ID 后继续验证。

## 2026-10-01 — Add guarded DingTalk event update and deletion

- **Work type:** Integrated production MCP code and configuration; real account writes were not attempted.
- **Problem:** Small-a could list and create DingTalk events but could not update or delete them. The user also needed a clear distinction between live hotel search and the non-live 12306 handoff.
- **Cause:** The narrow DingTalk bridge and active tool allowlist exposed only lookup and creation, even though the DingTalk Calendar API provides single-event GET, PUT, and DELETE operations.
- **Change:** Added exact-ID event detail lookup, partial update for title/time/location/description, and delete for one primary-calendar event. Both writes re-read the event and require exact current title and start time; they reject non-organizer, recurring, and non-verifiably personal events. MCP annotations classify the new writes for approval, and the CLI approval prompt shows the target event and requested changes. Updated both server allowlists and setup documentation. No live DingTalk write was performed.
- **Verification method:** Compared method/path/partial-update shape with the DingTalk calendar SDK and API documentation; used mocked responses to test GET, PUT, DELETE, stale-event refusal, unsafe IDs, time validation, empty successful delete responses, secret-free requests, permission classification, stdio discovery, and approval display. Ran focused MCP tests and the full small-a suite.
- **Actual result:** Focused DingTalk/MCP/rail/Duffel suite passed 34 tests. After the final fail-closed attendee check, focused DingTalk/MCP tests passed 25/25 and the final full suite passed 141/141. No real account edit or deletion was attempted.
- **Current status:** Update and delete tools are integrated but not live-verified against the user's DingTalk account. The previously reported empty read from the configured primary calendar is still unresolved.
- **Remaining work:** Confirm a known event is visible under the configured Union ID and primary calendar, then—only with explicit approval—test an update and delete on a disposable personal event. If DingTalk omits attendee details for personal events, refine the preflight without weakening protection for shared meetings. Rail remains an official-site handoff, not a live availability API; Duffel hotel search requires Stays access and test mode returns simulated data.

## 2026-10-01 — Replace active Duffel Stays lookup with domestic FlyAI hotels

- **Work type:** Integrated production MCP configuration and read-only connector, plus a live read-only provider evaluation. No hotel order, payment, or account change was made.
- **Problem:** The user has a Duffel test token but no Stays access, so the active Duffel hotel tool could not meet the domestic hotel-search need. Keeping two hotel tools visible would also invite duplicate searches.
- **Cause:** The existing Duffel server exposed `search_available_stays` even though Stays requires separate account permission. Its international provider path did not address the user's preference for a domestic hotel source.
- **Change:** Added a narrow FlyAI/Fliggy hotel MCP server using the official pinned `@fly-ai/flyai-cli` `search-hotel` command. It validates inputs, returns at most 10 compact candidates (default 5), preserves official detail URLs, marks masked prices, refuses malformed/provider-error output, and exposes no booking or payment. The child process receives only an allowlist of environment variables. Pinned a project-local Node 22 runtime after global Windows Node 24 emitted valid hotel JSON but then crashed on shutdown. Both active/example configurations now expose Duffel flights only and FlyAI hotels only. An optional `FLYAI_API_KEY` enables a fuller account mode; no key gives limited trial mode. Updated setup instructions and `.env.example`.
- **Verification method:** Compared CLI arguments and hotel fields with the provider's official hotel reference. Ran mocked tests for config allowlists, read-only discovery, bounded output, masked prices, invalid dates, missing hotel list, secret isolation, and provider failure. Ran a real read-only Hangzhou hotel query through the Python wrapper on Node 22 and inspected the returned candidate count, links and masked-price flag against the provider JSON. Ran the full small-a test suite; added a separate real stdio startup check.
- **Actual result:** The real wrapper query returned 10 FlyAI candidate hotels, showed the requested two, provided an official detail URL, and identified a masked trial price. The final full suite, including real stdio startup, passed 148/148 tests. Duffel hotel lookup is no longer advertised in the active tool allowlist. No exact hotel room rate or availability was verified on the detail page.
- **Current status:** Domestic hotel lookup is integrated and live read-only search works on this machine in limited trial mode. Price strings such as `¥1xx` are not bookable quotations. Duffel flight search remains unchanged.
- **Remaining work:** The user can optionally obtain a FlyAI API key for higher quota or fuller results, then verify an example result and exact room terms in the official FlyAI/Fliggy interface. Installation on another machine requires `npm ci` in `aa_my_agent/mcp/flyai_cli` and a restart. Provider terms, quota and return fields may evolve and should be rechecked before relying on results commercially.

## 2026-10-01 — Switch active flight search from Duffel test mode to FlyAI

- **Work type:** Integrated production MCP connector and configuration; one live, read-only flight search using the locally configured FlyAI key. No order, payment, or other provider write was made.
- **Problem:** The active Duffel flight tool used a test token and returned sandbox offers, including the fictional Duffel Airways. The user wants domestic flight candidates without confusing test fares with real booking choices.
- **Cause:** The MCP allowlist still exposed Duffel flight lookup, while the existing FlyAI bridge exposed only hotels despite the same pinned CLI supporting `search-flight`.
- **Change:** Extended the FlyAI bridge to expose `search_flight_offers` alongside `search_domestic_hotels` under one `flyai-travel` MCP server. The flight tool requires an exact departure date, validates route and optional return/price/direct-only filters, invokes only the pinned `search-flight` CLI command, and returns bounded adult-price, itinerary segment and official-link fields. It does not expose raw provider payloads, credentials, booking or payment. Disabled `duffel-travel` in active and example configurations while retaining its code for a deliberate future switch-back. Updated the setup guide and example environment comments.
- **Verification method:** Checked CLI argument and result fields against the official FlyAI `search-flight` reference. Ran mocked tests for tool discovery and read-only classification, key forwarding without exposing Duffel credentials, compact result shape, invalid dates/routes, provider failure, and real stdio startup. Loaded the existing local FlyAI key without printing it and made one read-only Beijing–Shanghai search for 2026-10-15; inspected only result counts and field presence. Ran the complete small-a test suite.
- **Actual result:** The key-backed provider request returned 10 flight candidates. The bridge showed two requested candidates; the first had an unmasked price, itinerary segments and an official detail link. Final full suite passed 151/151 tests. The configured tool list has FlyAI flight and hotel search, with Duffel disabled. No fare, seat availability or schedule was independently checked on a booking page.
- **Current status:** FlyAI is the active flight and hotel candidate source; the local key is configured. Duffel sandbox offers are no longer available to the agent through the active MCP configuration. Flight results remain indicative until confirmed on the provider's booking page.
- **Remaining work:** Restart small-a so it reloads the MCP server configuration. Verify one user-facing query shows `mcp__flyai-travel__search_flight_offers` and not a Duffel tool. Before any real purchase, compare the candidate's exact fare, baggage, availability and flight time on the linked provider page. Watch FlyAI quota and any future output-schema changes.

## 2026-10-04 — Public-copy configuration and offline test portability

- **Work type:** Packaging and test adaptation in the standalone public copy only; original production code and personal configuration are unchanged.
- **Problem:** Shipping personal servers.json or npm runtime files would expose machine-specific setup and make the public source unnecessarily large. Seven existing tests failed after those files were intentionally excluded.
- **Cause:** Two configuration tests read the private active configuration; five mocked FlyAI subprocess tests still required real CLI and Node paths before reaching their mocks.
- **Change:** Keep only servers.example.json, enable travel-demo by default, and leave providers opt-in. Configuration tests now check the public template, disabled providers and DingTalk credential gates. The five mocked tests create inert temporary CLI/Node files and patch only runtime paths; their subprocess, output, error and credential-isolation assertions remain. Correct setup documentation to require explicit enablement and use platform-appropriate npm/npx commands.
- **Verification method:** Run the entire copied offline suite, including in-process MCP discovery and real local stdio process discovery. Compare package Python files with the original project and check public release inputs for personal settings, runtime directories and local credential matches.
- **Actual result:** 210 tests passed with one existing dependency deprecation warning in 26.33 seconds. 115 copied package Python files are byte-identical to the original; only the two configuration/FlyAI test files differ. No npm runtime was copied, no live provider call was made, and no real calendar event was read or written.
- **Current status:** Public-copy packaging and local offline verification completed. These settings do not replace the original user's active MCP configuration. Historical live results above remain historical.
- **Remaining work:** Fresh dependency installation and Windows/Linux CI execution have not been verified remotely. Users must explicitly install/enable optional providers and perform their own credential-backed checks before relying on them.
