"""Fuzzy matching of party names (and item names) against the database."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional

from rapidfuzz import fuzz, process

from . import config
from .models import MatchCandidate, Party

# Words that carry little identity ("Das Store" vs "Das Stores" vs "M/S Das Store")
_NOISE = {
    "m/s", "ms", "messrs", "the", "&", "and", "co", "company", "ltd", "limited", "pvt", "private",
    "store", "stores", "traders", "trading", "enterprise", "enterprises", "agency", "agencies",
}


@lru_cache(maxsize=65536)  # called for every master name on every rerun – 8,000 items x 10 lines
def normalize(name: str) -> str:
    s = (name or "").lower()
    s = s.replace("&", " and ")
    # Dots become spaces so "P.Roy" == "P. Roy" == "P Roy"; core() re-merges runs of initials
    # ("a g store" -> "ag store"), so "A.G. Store" still equals "AG Store".
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def core(name: str) -> str:
    """Normalised name with initials merged ("a g" -> "ag") and noise words removed."""
    toks = [t for t in _merge_initials(normalize(name).split()) if t not in _NOISE]
    return " ".join(toks) or normalize(name)


# Business-type words. Two names whose identity words agree but whose type words conflict
# ("Das Store" vs "Das Traders", "National Traders" vs "National Enterprises") are usually
# *different* firms – they must never auto-match. Values are the canonical (singular) form.
_TYPE_WORDS = {
    "store": "store", "stores": "store",
    "trader": "trader", "traders": "trader", "trading": "trader",
    "enterprise": "enterprise", "enterprises": "enterprise",
    "agency": "agency", "agencies": "agency",
    "co": "company", "company": "company",
    "ltd": "limited", "limited": "limited",
    "pvt": "private", "private": "private",
}


def _type_words(normalized: str) -> set[str]:
    return {_TYPE_WORDS[t] for t in normalized.split() if t in _TYPE_WORDS}


def _score(query: str, candidate: str, candidate_types: frozenset[str] = frozenset()) -> float:
    """Similarity 0-100. ``candidate_types`` = type words of the party the label belongs to
    (relevant when ``candidate`` is an alias such as "Mohendra" for "Mohendra Traders")."""
    q, c = normalize(query), normalize(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 100.0
    cq, cc = core(query), core(candidate)
    s = max(
        fuzz.WRatio(q, c),
        fuzz.token_set_ratio(q, c),
        # core-vs-core catches "Das Store" ~ "Das Stores" ~ "M/S Das Store"
        fuzz.ratio(cq, cc) if cq and cc else 0,
    )
    cap = config.PARTY_AUTO_MATCH - 4  # below the auto threshold -> the user confirms
    tq, tc = cq.split(), cc.split()
    if cq != cc:
        # A single identifying word ("Roy" vs "P. Roy", "Ravi Store" vs "A.G. Store") is never a
        # confident match on its own.
        if len(tq) == 1 or len(tc) == 1:
            s = min(s, cap)
        # Extra identifying words on one side ("Das Store Bakery" vs "Das Store", "P Roy & Sons"
        # vs "P. Roy"): token_set_ratio scores a subset 100 – not confident either.
        if len(tq) != len(tc):
            s = min(s, cap)
    # Same identity words but conflicting business type ("Das Store" vs "Das Traders",
    # "Mohendra Store" vs alias "Mohendra" of "Mohendra Traders") -> confirm first. A type word on
    # one side only ("Mohendra Traders" on the bill, "Mohendra" in the books) is not a conflict.
    wq, wc = _type_words(q), _type_words(c) | candidate_types
    if wq and wc and wq.isdisjoint(wc):
        s = min(s, cap)
    # Numbers are identity: '20 Bn BSF' vs '200 Bn BSF' or '12 Wing Air Force' vs '14 Wing Air Force'
    # score 94-97 on characters alone, yet they are different units. When both sides carry
    # digit-bearing tokens and those differ, the user confirms (still offered as a suggestion).
    nq, nc = _numeric_tokens(q.split()), _numeric_tokens(c.split())
    if nq and nc and nq != nc:
        s = min(s, cap)
    return float(s)


def match_party(raw_name: Optional[str], parties: list[Party], limit: int = 5) -> list[MatchCandidate]:
    """Return candidates sorted by score (desc). Each party appears at most once."""
    if not raw_name or not raw_name.strip():
        return []
    best: dict[int, MatchCandidate] = {}
    for p in parties:
        name_types = frozenset(_type_words(normalize(p.name)))
        for label in [p.name, *p.aliases]:
            s = _score(raw_name, label, name_types)
            if p.id not in best or s > best[p.id].score:
                best[p.id] = MatchCandidate(party=p, score=s, matched_on=label)
    ranked = sorted(best.values(), key=lambda m: (-m.score, m.party.name.lower()))
    return [m for m in ranked if m.score >= config.PARTY_SUGGEST_MATCH][:limit]


def classify(cands: list[MatchCandidate]) -> str:
    """'auto' (confident single match), 'suggest' (needs confirmation) or 'new'."""
    if not cands:
        return "new"
    top = cands[0]
    if top.score >= config.PARTY_AUTO_MATCH:
        # ambiguous if a second candidate is nearly as good
        if len(cands) > 1 and cands[1].score >= top.score - 3 and cands[1].party.id != top.party.id:
            return "suggest"
        return "auto"
    return "suggest"


def match_item(raw_item: str, item_names: list[str]) -> Optional[str]:
    """Best matching canonical item name, or None if nothing is close enough.

    Deliberately strict: only spelling/punctuation variants snap ("rice bpt" -> "Rice (BPT)").
    Variants that carry extra tokens ("Rice (R.R 10)" vs "Rice (R.R)") are different items.
    """
    if not raw_item or not item_names:
        return None
    q = normalize(raw_item)
    if not q:
        return None
    # Runs of initials merge ('r r' -> 'rr') so 'Rice RR' / 'Rice R.R.' / 'Rice (R.R)' are one spelling.
    q_toks = _merge_initials(q.split())
    q_nums = _numeric_tokens(q_toks)
    q_joined = " ".join(q_toks)
    best, best_score = None, 0.0
    for name in item_names:
        c = normalize(name)
        if not c:
            continue
        if q == c:
            return name
        c_toks = _merge_initials(c.split())
        if len(q_toks) != len(c_toks):
            continue
        # Numbers are identity: 'Amaron 150 Ah Tubular' is not 'Amaron 130 Ah Tubular', '40R' is not
        # '40L', '1 Ltrs' is not '5 Ltrs' – one different digit scores 95 on a long name, so a
        # character ratio alone would silently book the wrong stock item.
        if _numeric_tokens(c_toks) != q_nums:
            continue
        # Every word must be a near-spelling of its counterpart: 'Tubuler' ~ 'Tubular' snaps, but
        # 'Offline' vs 'Online' or 'ACDB' vs 'DCDB' are different products even though the whole
        # string scores 90+ – those are offered as suggestions instead.
        if any(fuzz.ratio(a, b) < 80 for a, b in zip(q_toks, c_toks)):
            continue
        s = fuzz.ratio(q_joined, " ".join(c_toks))
        if s > best_score:
            best, best_score = name, s
    return best if best_score >= config.ITEM_AUTO_MATCH else None


def _merge_initials(tokens: list[str]) -> list[str]:
    """['rice', 'r', 'r', '10'] -> ['rice', 'rr', '10'] (same rule core() applies to party names)."""
    merged: list[str] = []
    last_was_initial = False
    for t in tokens:
        is_initial = len(t) == 1 and t.isalpha()
        if is_initial and last_was_initial:
            merged[-1] += t
        else:
            merged.append(t)
        last_was_initial = is_initial
    return merged


def _numeric_tokens(tokens: list[str]) -> tuple[str, ...]:
    """Sorted tokens that carry a digit ('150', '12v', '40l', '26-12' -> '26', '12')."""
    return tuple(sorted(t for t in tokens if any(ch.isdigit() for ch in t)))


@lru_cache(maxsize=8)
def _prepared_items(names: tuple[str, ...]) -> list[tuple[str, str, frozenset[str], frozenset[str]]]:
    """(name, normalised, tokens, real words) for every master name – built once per distinct list."""
    out = []
    for name in names:
        c = normalize(name)
        if not c:
            continue
        toks = frozenset(c.split())
        out.append((name, c, toks, frozenset(t for t in toks if len(t) >= 3 and not t.isdigit())))
    return out


def suggest_items(raw_item: str, item_names: list[str], limit: int = 3, min_score: Optional[int] = None) -> list[tuple[str, float]]:
    """Looser than match_item: ranked candidates for the user to confirm ('Rice RR 10' -> 'Rice (R.R 10)').
    Returns [(name, score)] with score >= AE_ITEM_SUGGEST_MATCH (default 70)."""
    if not raw_item or not item_names:
        return []
    q = normalize(raw_item)
    if not q:
        return []
    thr = config.ITEM_SUGGEST_MATCH if min_score is None else min_score
    q_toks = set(q.split())
    q_words = {t for t in q_toks if len(t) >= 3 and not t.isdigit()}
    scored: list[tuple[str, float]] = []
    for name, c, c_toks, c_words in _prepared_items(tuple(item_names)):
        # must share at least one real word – '10 & 12 Way MCB Box' sharing only '10' is noise
        if q_words and q_words.isdisjoint(c_words):
            continue
        s = max(fuzz.WRatio(q, c), fuzz.token_set_ratio(q, c))
        if q_toks > c_toks or c_toks > q_toks:
            s = min(s, 90.0)  # one is a strict superset of the other ('Rice (R.R 10)' vs 'Rice (R.R)') – close, not equal
        if len(q_toks) == 1 and len(c_toks) > 1:
            s = min(s, thr + 10)  # a bare generic word ('Rice') never confidently points at one variety
        if s >= thr:
            scored.append((name, float(s)))
    scored.sort(key=lambda t: (-t[1], t[0].lower()))
    return scored[:limit]
