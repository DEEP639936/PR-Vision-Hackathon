"""Evidence graph builder (spec #9).

Constructs the interactive graph data:
  Nodes: claim | article | source | fact_check | person | organization | event | image | document | url
  Edges: supports | contradicts | references | published_by | mentions | derived_from | related_to

Nodes carry stable keys within a job so the frontend can zoom/pan/select and
filter by edge type. Persistence: EvidenceEdge rows (see db/models/verification).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.evidence.fusion import ScoredEvidence
from app.evidence.ranking import host_independence_key


@dataclass
class GraphNode:
    key: str
    kind: str
    label: str
    props: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "label": self.label, **self.props}


@dataclass
class GraphEdge:
    source: str
    edge_type: str
    target: str
    weight: float = 1.0
    note: Optional[str] = None

    def __init__(self, source: str, edge_type: str, target: str, weight: float = 1.0, note: Optional[str] = None):
        self.source = source
        self.edge_type = edge_type
        self.target = target
        self.weight = weight
        self.note = note

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "edge_type": self.edge_type, "target": self.target,
                "weight": round(self.weight, 3), "note": self.note}


from typing import Optional  # noqa: E402  (kept after dataclass for readability)


class EvidenceGraphBuilder:
    """Accumulates nodes/edges while the pipeline walks claim→evidence→source."""

    def __init__(self, job_key: str) -> None:
        self.job_key = job_key
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        self._edge_seen: set[tuple[str, str, str]] = set()

    def add_node(self, key: str, kind: str, label: str, **props: Any) -> str:
        if key not in self.nodes:
            self.nodes[key] = GraphNode(key=key, kind=kind, label=label[:160], props=props)
        return key

    def add_edge(self, source: str, edge_type: str, target: str, weight: float = 1.0, note: Optional[str] = None) -> None:
        k = (source, edge_type, target)
        if k in self._edge_seen or source == target:
            return
        self._edge_seen.add(k)
        self.edges.append(GraphEdge(source, edge_type, target, weight, note))

    # ------------------------------------------------------------- wiring
    def add_claim(self, ordinal: int, text: str, claim_type: str, verdict: str, confidence: float) -> str:
        key = f"claim:{ordinal}"
        self.add_node(key, "claim", f"CLAIM {ordinal:02d} · {verdict}",
                      text=text, claim_type=claim_type, verdict=verdict, confidence=round(confidence, 3))
        return key

    def add_content(self, title: str, url: Optional[str], content_type: str) -> str:
        key = "content:main"
        self.add_node(key, "document" if content_type in ("pdf", "docx") else ("url" if content_type in ("article", "social_post") else "url"),
                      title or url or "Submitted content", url=url, content_type=content_type)
        return key

    def wire_claim_evidence(self, claim_key: str, evidence: list[ScoredEvidence], max_per_claim: int = 8) -> None:
        """CLAIM -> article/url nodes -> publisher (source) nodes."""
        for idx, ev in enumerate(evidence[:max_per_claim]):
            if not (ev.url or ev.title):
                continue
            ekey = f"evid:{abs(hash(ev.url or ev.title)) % 10_000_000}"
            label = ev.title or ev.url or "evidence"
            self.add_node(ekey, "article", label,
                          url=ev.url, provider=ev.provider, stance=ev.stance,
                          quality=round(ev.quality, 3), published_at=ev.published_at)
            edge_type = {"supports": "supports", "contradicts": "contradicts"}.get(ev.stance, "references")
            self.add_edge(claim_key, edge_type, ekey, weight=ev.quality)
            if ev.url:
                ukey = f"url:{host_independence_key(ev.url)}"
                self.add_node(ukey, "url", host_independence_key(ev.url))
                self.add_edge(ekey, "references", ukey, weight=0.8)
                if ev.publisher:
                    skey = f"source:{host_independence_key(ev.url)}"
                    self.add_node(skey, "source", ev.publisher or host_independence_key(ev.url),
                                  classification=ev.ranking.classification, quality=round(ev.quality, 3))
                    self.add_edge(ukey, "published_by", skey, weight=1.0)

    def wire_fact_check(self, claim_key: str, fact_checks: list[dict], ordinal: int) -> None:
        for idx, fc in enumerate(fact_checks[:4]):
            fkey = f"factcheck:{ordinal}:{idx}"
            label = f"{fc.get('publisher') or 'Fact-check'}: {fc.get('textual_rating') or 'review'}"
            self.add_node(fkey, "fact_check", label,
                          url=fc.get("url"), rating=fc.get("textual_rating"),
                          publisher=fc.get("publisher"), published_at=fc.get("published_at"))
            self.add_edge(claim_key, "related_to", fkey, weight=1.2, note="professional fact-check")
            if fc.get("publisher"):
                pkey = f"source:fc:{(fc.get('publisher') or 'unknown').lower()}"
                self.add_node(pkey, "source", fc["publisher"], classification="fact_checker")
                self.add_edge(fkey, "published_by", pkey, weight=1.0)

    def wire_entities(self, claim_key: str, entities: list[dict[str, str]]) -> None:
        for ent in entities[:6]:
            name = ent.get("name")
            if not name:
                continue
            ekey = f"entity:{name.lower()}"
            kind = "person" if ent.get("type") == "PERSON" else ("organization" if ent.get("type") == "ORGANIZATION" else "event")
            self.add_node(ekey, kind, name, entity_type=ent.get("type"))
            self.add_edge(claim_key, "mentions", ekey, weight=0.7)

    def wire_content_claims(self, content_key: str, claim_keys: list[str]) -> None:
        for ck in claim_keys:
            self.add_edge(content_key, "derived_from", ck, weight=1.0, note="claim extracted from content")

    def wire_media(self, content_key: str, media_label: str, media_meta: dict) -> str:
        mkey = "media:primary"
        self.add_node(mkey, "image" if media_meta.get("media_type") == "image" else "document",
                      media_label, **{k: media_meta.get(k) for k in ("sha256", "manipulation_risk", "ai_generation_signal")})
        self.add_edge(content_key, "derived_from", mkey, weight=1.0)
        return mkey

    def to_dict(self) -> dict[str, Any]:
        return {"nodes": [n.to_dict() for n in self.nodes.values()],
                "edges": [e.to_dict() for e in self.edges]}

    def persist(self, db, job_id: int) -> int:
        """Write edges (nodes are reconstructed client-side from edges+props)."""
        from app.db.models import EvidenceEdge
        import json as _json
        count = 0
        # persist node payloads inside edge note-less rows via a companion map:
        for e in self.edges:
            db.add(EvidenceEdge(
                job_id=job_id,
                source_kind=self.nodes.get(e.source, GraphNode(e.source, "url", e.source)).kind,
                source_key=e.source,
                edge_type=e.edge_type,
                target_kind=self.nodes.get(e.target, GraphNode(e.target, "url", e.target)).kind,
                target_key=e.target,
                weight=e.weight,
                note=e.note,
            ))
            count += 1
        # serialize the full node set once on a pseudo-edge from content root
        root = "content:main"
        db.add(EvidenceEdge(
            job_id=job_id, source_kind="meta", source_key="nodes",
            edge_type="graph_nodes", target_kind="meta", target_key=root,
            weight=0.0, note=_json.dumps([n.to_dict() for n in self.nodes.values()])[:4_000_000],
        ))
        return count
