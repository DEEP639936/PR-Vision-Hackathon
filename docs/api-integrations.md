# PR•VISION — Platform API Integrations

**Governing rule (spec #8):** PR•VISION integrates ONLY official platform APIs.
A metric a platform does not expose is stored as `NULL` — never fabricated,
never inferred from third-party scrapers. Connector health reports the REAL
state (`not_configured` when credentials are absent).

## Connector Interface

```python
class SocialPlatformConnector:
    async def fetch_posts(self, ...) -> list[NormalizedPost]
    async def fetch_post_metrics(self, post_id, since=None, post_posted_at=None) -> list[NormalizedMetrics]
    async def fetch_propagation_data(self, post_id, since=None) -> list[NormalizedPropagationEvent]
    async def health_check(self) -> ConnectorStatus
```

All adapters emit the normalized PR•VISION format; downstream code is
platform-agnostic.

## Capability Matrix (honest)

| Signal | X (Twitter) | Reddit | Instagram | Facebook | LinkedIn | Demo |
|---|---|---|---|---|---|---|
| Post content | ✅ | ✅ | ✅ (caption) | ✅ (message) | ✅ (commentary) | ✅ |
| Posting time | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Likes / reactions | ✅ like_count | ⚠️ score (net upvotes) | ✅ business accts | ✅ summary | ✅ reactionsSummary | ✅ |
| Comments / replies | ✅ reply_count | ✅ num_comments | ✅ | ✅ summary | ✅ commentsSummary | ✅ |
| Shares / reposts | ✅ retweet+quote | ⚠️ num_crossposts | ⚠️ Reels insights only | ✅ shares.count | ❌ removed by API | ✅ |
| Views / impressions | ✅ impression_count | ❌ | ⚠️ insights (v2+) | ❌ page-level only | ❌ partner-only | ✅ |
| Author followers | ✅ | ⚠️ subreddit subscribers | ✅ | ✅ fan_count | ✅ firstDegreeSize | ✅ |
| Unique sharers | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ (generated) |
| Propagation graph | ❌ (elevated/paid) | ❌ | ❌ | ❌ | ❌ | ✅ (generated) |

Legend: ✅ available · ⚠️ partial/conditional · ❌ not exposed by the official API.

## Platform Notes

### X (Twitter) — API v2
- Auth: app-only Bearer token (`X_BEARER_TOKEN`).
- Endpoint: `GET /2/tweets/search/recent` (recent search), `GET /2/tweets/{id}`.
- `shares` = retweet_count + quote_count (X's share mechanics).
- **Limitations:** reshare *graph* (who retweeted) requires elevated/paid access
  → propagation events are empty; unique sharers not exposed. Search covers the
  last 7 days on standard tiers. Rate limits vary per tier; `429` responses are
  honoured with `Retry-After` backoff.

### Reddit — official OAuth API
- Auth: application-only `client_credentials` (read-only public data).
- Endpoints: `GET /r/{sub}/new`, `GET /api/info?id=t3_…`.
- `score` (net upvotes) is used as the like analogue; `num_crossposts` as the
  closest share analogue (may be absent); `subreddit_subscribers` as the
  community-size follower proxy.
- **Limitations:** Reddit has no repost graph and no view counts; score is not
  raw likes. Conservative self-throttling (~1 req/s) plus 429 backoff.

### Instagram — Meta Graph API v21
- Auth: Meta token + business account (`META_ACCESS_TOKEN`,
  `META_INSTAGRAM_ACCOUNT_ID`).
- Endpoints: `GET /{ig-user-id}/media`, `GET /{media}/insights`.
- **Limitations:** like_count requires the business account relation; feed
  posts expose no share counts (Reels `shares` insight where eligible); views
  via `views` insight on eligible media; no propagation data.

### Facebook — Meta Graph API v21 (Pages)
- Auth: Page token with `pages_read_engagement` (`META_PAGE_ID`).
- Endpoints: `GET /{page-id}/posts` with `shares`,
  `comments.summary(true)`, `likes.summary(true)`.
- **Limitations:** per-post views unavailable (page-level insights only);
  no reshare graph; no unique sharers.

### LinkedIn — official REST (Community Management)
- Auth: OAuth2 token + organization URN (`LINKEDIN_ACCESS_TOKEN`,
  `LINKEDIN_ORGANIZATION_URN`).
- Endpoints: `GET /rest/posts`, `GET /rest/socialActions/{urn}`,
  `GET /rest/organizationalEntityFollowerStatistics`.
- **Limitations:** total share counts were removed from the official API →
  `shares = NULL`; impressions require Marketing-partner approval →
  `views = NULL`; no reshare graph.

### Demo provider
- Generates five behaviour archetypes (normal, trending, viral,
  suspicious_viral, false_alarm) with deterministic per-post timelines,
  realistic engagement ratios, and a reshare cascade.
- Flows through the **same** normalizer → database → features → ML pipeline as
  real connectors; posts are badged `is_demo=true` in the UI.

## Adding a New Platform

1. Subclass `SocialPlatformConnector` in `app/connectors/`.
2. Map only fields the official API actually returns; leave the rest `None`.
3. Register the adapter in `app/connectors/__init__.py` and the credential
   check in `Settings.is_platform_configured`.
4. Add the platform to the enum in `app/db/models/__init__.py` (no migration
   needed — platform is a validated string column).
5. Document capabilities and limitations in this file's matrix.
