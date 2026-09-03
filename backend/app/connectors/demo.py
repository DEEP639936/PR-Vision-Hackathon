"""Demo data provider — realistic temporal propagation without external APIs.

CRITICAL DESIGN GUARANTEE
-------------------------
Demo data flows through the *exact same* pipeline as real platform data:

    DemoConnector --> NormalizedPost/Metrics/Propagation --> MySQL
                  --> Feature engineering --> ML --> Intervention score

It is NOT a fake dashboard: the demo connector simply stands in for a platform
API. Posts carry `is_demo=True` so the UI can label them honestly.

Growth model
------------
Each post has a deterministic pseudo-random timeline derived from its
external id, so repeated polling continues the same curve seamlessly:

    velocity(t)  = archetype velocity shape  *  noise(seed, post_id, t)

Archetypes (spec #44):
    normal           slow propagation
    trending         moderate growth
    viral            rapid propagation
    suspicious_viral rapid propagation + misinfo-styled content
    false_alarm      rapid propagation + benign content
"""
from __future__ import annotations

import hashlib
import math
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.connectors.base import (
    ConnectorStatus,
    NormalizedMetrics,
    NormalizedPost,
    NormalizedPropagationEvent,
    SocialPlatformConnector,
)

ARCHETYPES = ("normal", "trending", "viral", "suspicious_viral", "false_alarm")

# ---------------------------------------------------------------- content pools
BENIGN_CONTENT = [
    "Just tried the new bakery on 5th avenue — the sourdough is genuinely incredible. Worth the queue!",
    "Our community garden hit 100 volunteers this weekend. Proud of this neighbourhood 🌱",
    "Match highlights: an absolute screamer from the edge of the box in the 89th minute. What a game!",
    "We are giving away 5 tickets to the science museum this Saturday. Reply with your favourite planet to enter.",
    "Sunny Sunday sketchdump — spent the morning drawing ducklings at the pond.",
    "The new library extension opens next month. Two extra floors of reading rooms and a coffee bar!",
    "PSA: the city marathon reroutes traffic around Elm street this year. Plan your morning commute.",
    "Tried restoring my grandfather's film camera. It still shoots beautifully after 60 years.",
]

VIRAL_BENIGN_CONTENT = [
    "BREAKING: local firefighter rescues parrot from storm drain, parrot immediately adopts him. Photos inside.",
    "This toddler correctly naming every dinosaur is the best thing you will see today. Sound ON.",
    "The double rainbow over the harbour right now is unreal. No filter.",
    "A whale just surfaced beside our ferry and the entire boat went silent. Nature is majestic.",
    "Grandma just won the village bake-off for the 9th year running. The recipe stays secret forever.",
]

SUSPICIOUS_CONTENT = [
    "BREAKING!!! Doctors DON'T want you to know this miracle cure — one teaspoon of this household item DESTROYS viruses instantly. They are hiding the SHOCKING truth!!! Share before it gets DELETED!!!",
    "EXPOSED: leaked document PROVES the government is putting mind-control chemicals in the water supply. The mainstream media will NOT report this. SHARE EVERYWHERE before we get silenced!!!",
    "URGENT WARNING!!! 5G towers just activated near schools — thousands of children already sick. Nurses are speaking out and being ERASED from the internet. Forward this to every parent NOW!!!",
    "SHOCKING: this one weird trick pays off your mortgage in 30 days — banks are FURIOUS it got leaked. Click before they take it down!!!",
    "THEY DON'T WANT YOU TO KNOW: famous celebrity secretly arrested for trafficking!! Evidence attached. Media blackout confirmed by insiders. SHARE before it disappears!!!",
    "Absolutely INFURIATING: supermarkets secretly spraying vegetables with a chemical BANNED in 43 countries!! A whistleblower risked everything to tell us. Spread the word!!!",
]

FALSE_ALARM_CONTENT = [
    "URGENT: reports of an explosion downtown — fire brigade confirms it was a controlled demolition at the old mill. All clear, no injuries. Sharing for anyone who heard the bang.",
    "Heads up: a message is circulating that the water is unsafe tonight. The utility confirms it was a FALSE ALARM — scheduled maintenance only. Water is perfectly safe.",
    "There is a viral rumour that the bridge is closed forever — it is NOT. It reopens Monday after inspection. Confirming with the council now.",
    "Panic in the group chats: school 'lockdown' messages are false — it was a fire drill scheduled all week. The headmaster's statement is attached.",
]


def _seed(post_key: str) -> int:
    return int(hashlib.sha256(post_key.encode()).hexdigest()[:12], 16)


def _parse_archetype(external_post_id: str) -> str:
    parts = external_post_id.split("_")
    if len(parts) >= 2 and parts[1] in ARCHETYPES:
        return parts[1]
    return "normal"


# Per-archetype velocity curves: v(t) = base + growth*t^power until peak, then decay.
_ARCH_PARAMS = {
    "normal":           dict(base=0.30, growth=0.008, power=1.0, peak_min=40,  decay=0.002,
                             like_ratio=3.5, comment_ratio=0.35, view_ratio=14.0, uniq_ratio=0.82),
    "trending":         dict(base=0.80, growth=0.045, power=1.0, peak_min=70,  decay=0.004,
                             like_ratio=4.0, comment_ratio=0.60, view_ratio=18.0, uniq_ratio=0.78),
    "viral":            dict(base=2.00, growth=0.900, power=1.0, peak_min=45,  decay=0.020,
                             like_ratio=4.5, comment_ratio=0.90, view_ratio=24.0, uniq_ratio=0.70),
    "suspicious_viral": dict(base=2.60, growth=1.500, power=1.0, peak_min=35,  decay=0.028,
                             like_ratio=2.2, comment_ratio=1.40, view_ratio=20.0, uniq_ratio=0.55),
    "false_alarm":      dict(base=2.20, growth=1.100, power=1.0, peak_min=40,  decay=0.024,
                             like_ratio=4.8, comment_ratio=1.10, view_ratio=26.0, uniq_ratio=0.72),
}

_CONTENT_BY_ARCH = {
    "normal": BENIGN_CONTENT,
    "trending": BENIGN_CONTENT + VIRAL_BENIGN_CONTENT,
    "viral": VIRAL_BENIGN_CONTENT,
    "suspicious_viral": SUSPICIOUS_CONTENT,
    "false_alarm": FALSE_ALARM_CONTENT,
}


def _velocity(t_minutes: float, params: dict, rng: random.Random) -> float:
    if t_minutes <= params["peak_min"]:
        v = params["base"] + params["growth"] * (t_minutes ** 1.0)
    else:
        peak_v = params["base"] + params["growth"] * params["peak_min"]
        v = max(0.35, peak_v * math.exp(-params["decay"] * (t_minutes - params["peak_min"])))
    return v * max(0.35, rng.gauss(1.0, 0.16))


def _noise(post_key: str, bucket: int) -> random.Random:
    return random.Random(_seed(f"{post_key}:{bucket}"))


def _shares_at(post_key: str, archetype: str, minutes_elapsed: float) -> float:
    """Deterministic cumulative shares at `minutes_elapsed` (integrated velocity)."""
    params = _ARCH_PARAMS[archetype]
    total = 0.0
    step = 1.0  # minute resolution
    t = 0.0
    while t < minutes_elapsed:
        rng = _noise(post_key, int(t))
        total += _velocity(t, params, rng) * step
        t += step
    return total


def _timeline(
    external_post_id: str,
    archetype: str,
    posted_at: datetime,
    start: datetime,
    end: datetime,
    step_seconds: int,
) -> list[NormalizedMetrics]:
    """Metric snapshots from `start` to `end` at a fixed cadence.

    Cumulative counters (shares/likes/…) are monotone non-decreasing: the
    deterministic curve is shared and only *rates* carry noise.
    """
    params = _ARCH_PARAMS[archetype]
    out: list[NormalizedMetrics] = []
    t = start
    while t <= end:
        minutes = max(0.0, (t - posted_at).total_seconds() / 60.0)
        rng = _noise(f"{external_post_id}:snap", int(t.timestamp() // max(step_seconds, 1)))
        shares = max(0, round(_shares_at(external_post_id, archetype, minutes)))
        jitter = rng.uniform(0.90, 1.10)
        likes = max(0, round(shares * params["like_ratio"] * jitter + rng.uniform(0, 6)))
        comments = max(0, round(shares * params["comment_ratio"] * jitter))
        views = max(0, round(shares * params["view_ratio"] * jitter + minutes * rng.uniform(4, 18)))
        followers = max(50, round(rng.uniform(400, 80_000) * (3 if "viral" in archetype else 1)))
        unique_sharers = max(0, round(shares * params["uniq_ratio"] * jitter))
        out.append(
            NormalizedMetrics(
                timestamp=t,
                likes=likes,
                comments=comments,
                shares=shares,
                views=views,
                followers=followers,
                unique_sharers=unique_sharers,
            )
        )
        t += timedelta(seconds=step_seconds)
    return out


def _propagation_events(
    external_post_id: str,
    archetype: str,
    posted_at: datetime,
    now: datetime,
    max_events: int = 350,
) -> list[NormalizedPropagationEvent]:
    """Generate a reshare cascade whose density follows the share velocity curve."""
    params = _ARCH_PARAMS[archetype]
    minutes_total = max(1.0, (now - posted_at).total_seconds() / 60.0)
    n_events = min(max_events, max(3, int(_shares_at(external_post_id, archetype, minutes_total) * 0.35)))
    rng = random.Random(_seed(f"{external_post_id}:prop"))
    events: list[NormalizedPropagationEvent] = []

    # Depth distribution — suspicious content tends to spread in tighter chains.
    for i in range(n_events):
        minutes = min(minutes_total, minutes_total * (i / n_events) ** 0.85 * rng.uniform(0.9, 1.1))
        ts = posted_at + timedelta(minutes=minutes)
        depth = rng.choices([1, 2, 3], weights=[62, 27, 11])[0]
        events.append(
            NormalizedPropagationEvent(
                source_user_id=f"u{rng.randrange(10_000, 99_999)}",
                target_user_id=f"u{rng.randrange(10_000, 99_999)}",
                event_type=rng.choices(["share", "repost", "quote"], weights=[70, 20, 10])[0],
                timestamp=ts,
                time_since_original_post=minutes * 60.0,
                depth=depth,
            )
        )
    events.sort(key=lambda e: e.timestamp)
    return events


class DemoConnector(SocialPlatformConnector):
    """Generates realistic demo data through the standard connector interface."""

    platform = "demo"

    async def generate_post(self, *, archetype: Optional[str] = None, age_minutes: Optional[int] = None) -> tuple[NormalizedPost, list[NormalizedMetrics], list[NormalizedPropagationEvent]]:
        """Create a new demo post with full backfilled history up to now."""
        archetype = archetype if archetype in ARCHETYPES else random.choice(ARCHETYPES)
        token = uuid.uuid4().hex[:8]
        external_post_id = f"demo_{archetype}_{token}"
        rng = random.Random(_seed(external_post_id))
        now = datetime.now(timezone.utc)
        # Mix of fresh and mature posts so long-horizon anchors (t+120m) exist
        # for training and older posts demonstrate decay dynamics.
        age = age_minutes if age_minutes is not None else rng.choice(
            [rng.randint(35, 110), rng.randint(35, 110), rng.randint(150, 265)])
        posted_at = now - timedelta(minutes=age)

        content_pool = _CONTENT_BY_ARCH[archetype]
        content = rng.choice(content_pool)
        followers = rng.randint(800, 250_000) if archetype != "normal" else rng.randint(300, 6_000)

        post = NormalizedPost(
            platform=self.platform,
            post_id=external_post_id,
            author_id=f"user_{rng.randrange(1000, 9999)}",
            author_display_name=f"Demo User {rng.randrange(10, 99)}",
            content=content,
            posted_at=posted_at,
            url=f"https://demo.prvision.local/{external_post_id}",
            language="en",
            is_demo=True,
            followers=followers,
        )
        snapshots = _timeline(external_post_id, archetype, posted_at, posted_at, now, step_seconds=120)
        events = _propagation_events(external_post_id, archetype, posted_at, now)
        # The post's at-creation metrics come from the first timeline point so
        # the initial snapshot (stored by ingestion) carries real values, not NULLs.
        first = snapshots[0] if snapshots else None
        post.likes = first.likes if first else 0
        post.comments = first.comments if first else 0
        post.shares = first.shares if first else 0
        post.views = first.views if first else 0
        post.unique_sharers = first.unique_sharers if first else 0
        return post, snapshots, events

    async def fetch_posts(self, *, limit: int = 20, **kwargs) -> list[NormalizedPost]:
        posts: list[NormalizedPost] = []
        for _ in range(min(limit, 20)):
            post, _snaps, _ev = await self.generate_post()
            posts.append(post)
        return posts

    async def fetch_post_metrics(
        self,
        post_id: str,
        *,
        since: Optional[datetime] = None,
        post_posted_at: Optional[datetime] = None,
    ) -> list[NormalizedMetrics]:
        """Continue the deterministic timeline from `since` (or last 15 min) to now.

        Requires `post_posted_at` (the true post creation time from the DB) so
        the curve integrates from the same origin as the initial backfill.
        """
        archetype = _parse_archetype(post_id)
        now = datetime.now(timezone.utc)
        if post_posted_at is None:
            post_posted_at = now - timedelta(minutes=30)  # conservative default
        # DB reads may yield naive datetimes (SQLite/MySQL) — normalise to UTC.
        if post_posted_at.tzinfo is None:
            post_posted_at = post_posted_at.replace(tzinfo=timezone.utc)
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        start = max(since or (now - timedelta(minutes=15)), post_posted_at)
        if start >= now:
            return []
        return _timeline(post_id, archetype, post_posted_at, start, now, step_seconds=120)

    async def fetch_propagation_data(self, post_id: str, *, since: Optional[datetime] = None) -> list[NormalizedPropagationEvent]:
        archetype = _parse_archetype(post_id)
        now = datetime.now(timezone.utc)
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        start = since or (now - timedelta(minutes=15))
        events = _propagation_events(post_id, archetype, start, now, max_events=40)
        return events

    async def health_check(self) -> ConnectorStatus:
        return ConnectorStatus(
            platform=self.platform, configured=True, healthy=True,
            detail="demo provider ready (no external API required)",
        )
