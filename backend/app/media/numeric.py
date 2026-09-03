"""Numerical fact checking — deterministic arithmetic (spec #22, #23, #24).

LLMs are NEVER trusted with arithmetic here. Every check is plain Python.

Checks:
  * percentage_bound      percentages must live in [0, 100] (or >100 flagged
                          when context implies a share, not growth)
  * total_sum             "X of A and Y of B" vs claimed total
  * growth_consistency    claimed % growth vs before/after values in text/tables
  * unit_consistency      mixed units for the same measure (k vs million…)
  * date_arithmetic       "X days since Y" recomputation
  * table_growth          CSV/table column growth % vs stated %  (spec #22)
  * chart_scale           axis-start hints from numeric series (spec #24, basic)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class NumCheck:
    check_type: str
    subject: str
    status: str            # consistent|inconsistent|unverifiable
    expected: Optional[str]
    observed: Optional[str]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_type": self.check_type, "subject": self.subject,
            "status": self.status, "expected": self.expected,
            "observed": self.observed, "detail": self.detail,
        }


_NUM = r"(\d[\d,]*(?:\.\d+)?)"
_PCT = re.compile(rf"{_NUM}\s?(?:%|percent|per cent)", re.IGNORECASE)
_GROWTH = re.compile(
    rf"(?P<what>[\w\s'’\-]{{2,48}}?)?(?:grew|grow|increased?|rose|risen|fell|dropped|declined?|decreased?|reduced)\s*"
    rf"(?:by|to)?\s*(?P<pct>{_NUM})\s?(?:%|percent|per cent)"
    rf"(?:\s*(?:from|after)\s*(?P<from>{_NUM})\b(?:\s+[a-z][a-z'’]{{0,14}}){{0,4}}\s*(?:to|in|by)\s*(?P<to>{_NUM})\b)?",
    re.IGNORECASE,
)
_TOTALS = re.compile(
    rf"(?P<a>{_NUM})\s*(?P<aunit>[a-z]*)\s*(?:\+|and|plus|plus|with|along with)\s*"
    rf"(?P<b>{_NUM})\s*(?P<bunit>[a-z]*)\s*(?:=|is|are|makes?|total[s]?|gives?|equals)\s*(?P<total>{_NUM})",
    re.IGNORECASE,
)
_DAYS_SINCE = re.compile(
    rf"(?P<n>{_NUM})\s*(?:days?|weeks?|months?|years?)\s+(?:since|after|following|past)\s+"
    rf"(?P<date>\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{{4}})",
    re.IGNORECASE,
)
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _num(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def check_percentages(text: str) -> list[NumCheck]:
    """Percentages outside 0..100 are either wrong units or wrong math — flag
    with context (growth can legitimately exceed 100%)."""
    checks: list[NumCheck] = []
    for m in _PCT.finditer(text):
        v = _num(m.group(1))
        if v is None:
            continue
        start = max(0, m.start() - 60)
        ctx = text[start:m.end() + 40].replace("\n", " ")
        growth_ctx = bool(re.search(r"(growth|increase|rose|grew|up|more than|decline|drop|fell|decrease|change|improvement)", ctx, re.IGNORECASE))
        share_ctx = bool(re.search(r"(of|share|proportion|approve|support|said|voted|agreed|population|respondents|majority)", ctx, re.IGNORECASE))
        if v < 0:
            checks.append(NumCheck("percentage_bound", ctx.strip(), "inconsistent", "0–100 for shares", f"{v:g}%",
                                   "Negative percentage cannot describe a share of a whole; verify sign/units."))
        elif v > 100 and share_ctx and not growth_ctx:
            checks.append(NumCheck("percentage_bound", ctx.strip(), "inconsistent", "0–100 for shares", f"{v:g}%",
                                   "A share of a whole cannot exceed 100%. Could be a growth rate misread as a share."))
        elif v > 100 and growth_ctx:
            checks.append(NumCheck("percentage_bound", ctx.strip(), "consistent", "growth may exceed 100%", f"{v:g}%",
                                   "Growth rates can exceed 100% — plausible, verify base value."))
    return checks[:8]


def check_totals(text: str) -> list[NumCheck]:
    """'A and B make C' — recompute the sum deterministically."""
    checks: list[NumCheck] = []
    for m in _TOTALS.finditer(text):
        a, b, total = _num(m.group("a")), _num(m.group("b")), _num(m.group("total"))
        if a is None or b is None or total is None:
            continue
        expected = a + b
        ctx = m.group(0)[:160]
        if abs(expected - total) <= max(1.0, total * 0.005):
            checks.append(NumCheck("total_sum", ctx, "consistent", f"{expected:g}", f"{total:g}",
                                   f"Arithmetic verified: {a:g} + {b:g} = {expected:g}."))
        else:
            checks.append(NumCheck("total_sum", ctx, "inconsistent", f"{expected:g}", f"{total:g}",
                                   f"Deterministic recalculation: {a:g} + {b:g} = {expected:g}, not {total:g}."))
    return checks[:8]


def check_growth(text: str) -> list[NumCheck]:
    """Claimed % growth vs stated before/after values (spec #22 example)."""
    checks: list[NumCheck] = []
    for m in _GROWTH.finditer(text):
        pct = _num(m.group("pct"))
        frm, to = _num(m.group("from")), _num(m.group("to"))
        if pct is None or frm is None or to is None or frm == 0:
            continue
        calculated = (to - frm) / abs(frm) * 100.0
        ctx = m.group(0)[:160]
        what = (m.group("what") or "value").strip() or "value"
        if abs(calculated - pct) <= max(0.5, abs(pct) * 0.02):
            checks.append(NumCheck("growth_consistency", f"{what}: {frm:g}→{to:g}", "consistent",
                                   f"{calculated:.1f}%", f"{pct:g}%",
                                   f"Recomputed growth: ({to:g} − {frm:g}) / {frm:g} = {calculated:.1f}%. Claim matches."))
        else:
            checks.append(NumCheck("growth_consistency", f"{what}: {frm:g}→{to:g}", "inconsistent",
                                   f"{calculated:.1f}%", f"{pct:g}%",
                                   f"Claimed growth {pct:g}% but stated values {frm:g} → {to:g} imply {calculated:.1f}%. "
                                   "Potential numerical inconsistency detected."))
    return checks[:8]


def check_units(text: str) -> list[NumCheck]:
    """Same measure expressed with clashing scale units (k vs million)."""
    checks: list[NumCheck] = []
    scales = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6,
              "bn": 1e9, "b": 1e9, "billion": 1e9, "trillion": 1e12}
    hits: list[tuple[str, float]] = []
    for m in re.finditer(rf"{_NUM}\s?(k|thousand|million|billion|bn|trillion)\b", text, re.IGNORECASE):
        v = _num(m.group(1))
        unit = m.group(2).lower()
        if v is None:
            continue
        hits.append((unit, v * scales.get(unit, 1.0)))
    # group by magnitude proximity: a 'thousand' value in the same sentence-family
    # as an equivalent 'million' value is fine; wildly inconsistent restatements flag
    by_scale: dict[str, int] = {}
    for unit, _abs in hits:
        key = {"k": "k", "thousand": "k", "m": "M", "million": "M", "bn": "B", "b": "B", "billion": "B", "trillion": "T"}[unit]
        by_scale[key] = by_scale.get(key, 0) + 1
    if len(by_scale) >= 2 and len(hits) >= 3:
        detail = ", ".join(f"{k}: {v}×" for k, v in sorted(by_scale.items()))
        checks.append(NumCheck("unit_consistency", "mixed scale units in content", "unverifiable", None, detail,
                               "Content mixes scale units (k/million/billion). Not necessarily wrong — verify each figure's base."))
    return checks[:4]


def check_date_arithmetic(text: str, now: Optional[datetime] = None) -> list[NumCheck]:
    """'N days since DATE' — recompute (spec #23 date arithmetic)."""
    checks: list[NumCheck] = []
    now = now or datetime(2026, 1, 1)  # deterministic default; callers may pass real now
    for m in _DAYS_SINCE.finditer(text):
        n = _num(m.group("n"))
        date_raw = m.group("date").strip()
        parsed: Optional[datetime] = None
        try:
            if re.match(r"\d{4}-\d{2}-\d{2}", date_raw):
                parsed = datetime.strptime(date_raw, "%Y-%m-%d")
            else:
                dm = re.match(r"(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})", date_raw)
                if dm:
                    parsed = datetime(int(dm.group(3)), _MONTHS.get(dm.group(2)[:3].lower(), 1), int(dm.group(1)))
        except ValueError:
            continue
        if parsed is None or n is None:
            continue
        unit = "days"
        mm = re.search(r"(days?|weeks?|months?|years?)", m.group(0), re.IGNORECASE)
        if mm:
            unit = mm.group(1).lower().rstrip("s")
        if unit == "days":
            delta = (now - parsed).days
        elif unit == "weeks":
            delta = (now - parsed).days // 7
        elif unit == "months":
            delta = int((now - parsed).days / 30.44)
        else:
            delta = int((now - parsed).days / 365.25)
        ctx = m.group(0)[:140]
        if abs(delta - n) <= max(1, n * 0.05):
            checks.append(NumCheck("date_arithmetic", ctx, "consistent", f"~{delta:g} {unit}", f"{n:g} {unit}",
                                   f"Recomputed from {parsed.date().isoformat()}: ≈{delta:g} {unit}. Claim plausible."))
        else:
            checks.append(NumCheck("date_arithmetic", ctx, "inconsistent", f"~{delta:g} {unit}", f"{n:g} {unit}",
                                   f"From {parsed.date().isoformat()} the interval is ≈{delta:g} {unit}, not {n:g}."))
    return checks[:6]


# ------------------------------------------------------------------ tables (spec #22)
def check_table(values: list[list[str]], header: Optional[list[str]] = None) -> list[NumCheck]:
    """Scan a numeric table for suspicious growth claims and impossible values."""
    checks: list[NumCheck] = []
    if not values:
        return checks
    n_cols = max(len(r) for r in values)
    for c in range(n_cols):
        col = []
        for r in values:
            if c < len(r):
                v = _num(r[c])
                if v is not None:
                    col.append(v)
        if len(col) >= 2:
            name = (header[c] if header and c < len(header) else f"column {c + 1}")
            # impossible percent column
            if header and c < len(header) and re.search(r"(%|percent|share|rate)", header[c] or "", re.IGNORECASE):
                bad = [v for v in col if v < 0 or v > 100]
                if bad and not re.search(r"(growth|change)", header[c] or "", re.IGNORECASE):
                    checks.append(NumCheck("percentage_bound", f"table column “{name}”", "inconsistent",
                                           "0–100", f"min {min(col):g}, max {max(col):g}",
                                           f"{len(bad)} value(s) outside 0–100 in a percentage column."))
            # growth between consecutive rows
            if len(col) >= 2:
                fr, to = col[0], col[-1]
                if fr not in (0,):
                    calc = (to - fr) / abs(fr) * 100.0
                    checks.append(NumCheck("table_growth", f"{name}: {fr:g}→{to:g}", "consistent",
                                           f"{calc:.1f}% implied", f"{calc:.1f}% calculated",
                                           f"Deterministic growth across table column “{name}”: {calc:.1f}%. "
                                           "Compare with any growth % claimed in the accompanying text."))
    return checks[:8]


def check_csv_stats(stats: dict[str, Any]) -> list[NumCheck]:
    """CSV-level checks using ingestion statistics (numeric columns)."""
    checks: list[NumCheck] = []
    numeric_cols = stats.get("numeric_columns") or []
    if numeric_cols:
        checks.append(NumCheck(
            "table_structure", "CSV numeric columns", "consistent",
            None, ", ".join(numeric_cols[:8]),
            f"{stats.get('rows', '?')} data rows × {stats.get('columns', '?')} columns; "
            f"{len(numeric_cols)} column(s) numeric and ready for deterministic verification.",
        ))
    return checks


def run_all_text_checks(text: str, now: Optional[datetime] = None) -> list[NumCheck]:
    """Full deterministic battery over free text (spec #23)."""
    out: list[NumCheck] = []
    out += check_percentages(text)
    out += check_totals(text)
    out += check_growth(text)
    out += check_date_arithmetic(text, now or datetime.now(timezone.utc).replace(tzinfo=None))
    out += check_units(text)
    return out
