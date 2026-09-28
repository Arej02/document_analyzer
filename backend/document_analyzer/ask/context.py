"""Deterministic context retrieval for one job (no LLM involvement).

Pipeline (Galvanise's ContextEngine, adapted to Supabase): semantic search
(`match_source_chunks`, pgvector cosine, pinned MiniLM) + symbol/path search over the
job's entity names -> reciprocal rank fusion -> seed entities -> bounded two-hop graph
expansion over `entity_edges` (both directions, per-node cap for hubs) -> excerpts of
discovered entities -> merge overlapping excerpts of a file -> token budget.

Every query goes through the caller's RLS-scoped client and filters on the job id, so
evidence can only come from the requesting org's job.
"""

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from document_analyzer.worker.evidence import estimate_tokens

MAX_QUESTION_CHARS = 2000
MAX_TERMS = 8
PAGE_SIZE = 1000
ID_CHUNK = 200
ASSOC_PRIORITY = {"entity_range": 0, "starts_in": 1}
RELATIONSHIP_SHARE = 0.25

STOPWORDS = frozenset(
    """a an and any are as at be been by can could do does did for from has have how i if in
    into is it its itself me my of on or our please show tell that the their them then there these
    they this those to up was we what when where which while who whom why will with would you your
    handled handle handles handling work works working process processes function functions method
    methods module modules component components code codebase call calls called calling depend
    depends dependency dependencies direct directly reference references referenced use uses used
    using about explain describe find locate implemented implementation defined define happen
    happens part parts file files class classes thing things other same name named different
    """.split()
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[./\\:][A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?")
_SUFFIXES = ("ations", "ation", "ments", "ment", "ings", "ing", "ers", "ed", "es", "s")


class QuestionError(ValueError):
    """The question is empty or too long."""


@dataclass
class Limits:
    semantic_candidates: int = 20
    symbol_candidates: int = 20
    seed_entities: int = 8
    hops: int = 2
    per_node_edges: int = 12
    max_edges: int = 60
    max_expanded_nodes: int = 40
    evidence_tokens: int = 12_000
    rrf_k: int = 60


@dataclass
class EntityRef:
    entity_id: str
    name: str
    entity_type: str
    file_path: str
    line: int | None = None
    association: str | None = None


@dataclass
class Evidence:
    evidence_id: str
    file_path: str
    line_start: int
    line_end: int
    content: str
    origins: list[str]
    why: list[str]
    entities: list[EntityRef]
    chunk_ids: list[str]
    key: str = ""


@dataclass
class Relationship:
    relationship_id: str
    edge_id: str
    edge_type: str
    source: EntityRef
    target: EntityRef
    provenance: dict[str, Any]
    hop: int


@dataclass
class Context:
    question: str
    job_id: str
    evidence: list[Evidence]
    relationships: list[Relationship]
    limitations: list[str]
    terms: list[str]
    semantic_used: bool
    truncated: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)


def extract_terms(question: str) -> list[str]:
    """Bounded lexical terms: code-looking identifiers first, then content words (+ stems)."""
    code, words = [], []
    for match in _IDENT.finditer(question):
        tok = match.group(0)
        bare = tok.removesuffix("()")
        if bare.lower() in STOPWORDS or len(bare) < 3:
            continue
        looks_code = (
            tok.endswith("()")
            or any(c in bare for c in "_./\\:")
            or re.search(r"[a-z][A-Z]", bare) is not None
            or (match.start() > 0 and question[match.start() - 1] == "`")
        )
        (code if looks_code else words).append(bare[:64])
    out: list[str] = []
    for term in code + words:
        if term.lower() not in (t.lower() for t in out):
            out.append(term)
    for term in list(words):
        low = term.lower()
        for suffix in _SUFFIXES:
            if low.endswith(suffix) and len(low) - len(suffix) >= 4:
                stem = term[: len(term) - len(suffix)]
                if stem.lower() not in (t.lower() for t in out):
                    out.append(stem)
                break
    return out[:MAX_TERMS]


def match_class(term: str, entity: dict[str, Any]) -> int:
    """0 exact, 1 prefix, 2 substring of the entity's own name; 3 matched only through the
    file path; 4 no match. A path match ranks below every name match, otherwise every
    entity of `billing.php` would outrank real name matches for "billing"."""
    t = term.lower().split("::")[-1].split(".")[-1].split("\\")[-1]
    name = (entity.get("name") or "").lower()
    if name == t:
        return 0
    if name.startswith(t):
        return 1
    if t in name:
        return 2
    return 3 if term.lower() in (entity.get("file_path") or "").lower() else 4


def _ref(entity: dict[str, Any], association: str | None = None) -> EntityRef:
    facts = entity.get("raw_facts") or {}
    line = facts.get("line") if isinstance(facts.get("line"), int) else None
    return EntityRef(
        entity_id=entity["id"],
        name=entity.get("name") or "",
        entity_type=entity.get("entity_type") or "",
        file_path=entity.get("file_path") or "",
        line=line,
        association=association,
    )


def _pages(query_factory: Callable[[], Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        page = query_factory().range(start, start + PAGE_SIZE - 1).execute().data
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE


def _in_chunks(ids: list[str]):
    for i in range(0, len(ids), ID_CHUNK):
        yield ids[i : i + ID_CHUNK]


@dataclass
class _Candidate:
    chunk: dict[str, Any]
    origins: set[str] = field(default_factory=set)
    semantic_rank: int | None = None
    symbol_rank: int | None = None
    rrf: float = 0.0
    why: list[str] = field(default_factory=list)
    priority: tuple = ()


class _Store:
    """The handful of Agent 1 reads the context engine needs, all job-scoped."""

    def __init__(self, client, org_id: str, job_id: str):
        self.client = client
        self.org_id = org_id
        self.job_id = job_id

    def _table(self, name: str, columns: str):
        return self.client.table(name).select(columns).eq("org_id", self.org_id).eq("job_id", self.job_id)

    def entities(self) -> list[dict[str, Any]]:
        return _pages(lambda: self._table("extracted_entities", "id, entity_type, name, file_path, raw_facts").order("id"))

    def chunk_count(self) -> int:
        return len(self._table("source_chunks", "id").limit(1).execute().data)

    def semantic(self, vector: list[float], count: int) -> list[dict[str, Any]]:
        params = {"p_job_id": self.job_id, "p_query": vector, "p_match_count": count}
        return list(self.client.rpc("match_source_chunks", params).execute().data or [])

    def chunks(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for part in _in_chunks(ids):
            for row in (
                self._table("source_chunks", "id, file_path, line_start, line_end, strategy, content, token_estimate")
                .in_("id", part)
                .execute()
                .data
            ):
                out[row["id"]] = row
        return out

    def links(self, column: str, ids: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for part in _in_chunks(ids):
            rows.extend(
                self._table("source_chunk_entities", "chunk_id, entity_id, association").in_(column, part).execute().data
            )
        return rows

    def edges(self, column: str, ids: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for part in _in_chunks(ids):
            rows.extend(
                self._table("entity_edges", "id, from_entity_id, to_entity_id, edge_type, provenance")
                .in_(column, part)
                .execute()
                .data
            )
        return rows


def _best_chunk_per_entity(links: list[dict[str, Any]]) -> dict[str, str]:
    best: dict[str, tuple[int, str]] = {}
    for link in links:
        rank = ASSOC_PRIORITY.get(link["association"], 9)
        current = best.get(link["entity_id"])
        if current is None or (rank, link["chunk_id"]) < current:
            best[link["entity_id"]] = (rank, link["chunk_id"])
    return {entity_id: chunk_id for entity_id, (_rank, chunk_id) in best.items()}


def retrieve(
    client,
    org_id: str,
    job_id: str,
    question: str,
    embed: Callable[[str], list[float]] | None,
    limits: Limits | None = None,
    coverage: dict[str, Any] | None = None,
    timer: Callable[[], float] = time.perf_counter,
) -> Context:
    question = (question or "").strip()
    if not question:
        raise QuestionError("ask a question")
    if len(question) > MAX_QUESTION_CHARS:
        raise QuestionError(f"questions are limited to {MAX_QUESTION_CHARS} characters")
    limits = limits or Limits()
    store = _Store(client, org_id, job_id)
    timings: dict[str, float] = {}
    limitations: list[str] = []
    truncated: list[str] = []

    def ms(start: float) -> float:
        return round((timer() - start) * 1000, 2)

    if store.chunk_count() == 0:
        return Context(
            question=question,
            job_id=job_id,
            evidence=[],
            relationships=[],
            limitations=[
                "This job has no stored source evidence (it was ingested before evidence storage "
                "existed, or no file could be excerpted); reingest the project to ask questions"
            ],
            terms=[],
            semantic_used=False,
        )

    # 1. Semantic retrieval --------------------------------------------------------------
    t = timer()
    semantic_rows: list[dict[str, Any]] = []
    semantic_used = False
    if embed is not None:
        try:
            semantic_rows = store.semantic(embed(question), limits.semantic_candidates)
            semantic_used = True
        except Exception as exc:  # noqa: BLE001 — degrade to symbol search, and say so
            limitations.append(f"Semantic retrieval not used: {type(exc).__name__}")
    else:
        limitations.append("Semantic retrieval not used: no embedding model configured")
    timings["semantic_search"] = ms(t)

    # 2. Symbol / path retrieval ---------------------------------------------------------
    t = timer()
    terms = extract_terms(question)
    entities = {row["id"]: row for row in store.entities()}
    scored: list[tuple[int, int, str, str]] = []
    for term in terms:
        for entity in entities.values():
            cls = match_class(term, entity)
            if cls < 4:
                scored.append((cls, len(entity.get("name") or ""), entity["id"], term))
    scored.sort()
    symbols: list[tuple[str, str, int]] = []
    seen_symbols: set[str] = set()
    for cls, _length, entity_id, term in scored:
        if entity_id in seen_symbols:
            continue
        seen_symbols.add(entity_id)
        symbols.append((entity_id, term, cls))
        if len(symbols) >= limits.symbol_candidates:
            break
    timings["symbol_search"] = ms(t)

    # 3. Symbols -> chunks, 4. reciprocal rank fusion ------------------------------------
    t = timer()
    symbol_chunk = _best_chunk_per_entity(store.links("entity_id", [s[0] for s in symbols]))
    candidates: dict[str, _Candidate] = {}
    for rank, row in enumerate(semantic_rows, start=1):
        c = candidates.setdefault(row["id"], _Candidate(row))
        c.origins.add("semantic")
        c.semantic_rank = rank
        c.why.append(f"semantic match #{rank} (cosine distance {float(row.get('distance') or 0):.3f})")
    needed = [symbol_chunk[s[0]] for s in symbols if s[0] in symbol_chunk and symbol_chunk[s[0]] not in candidates]
    chunk_rows = store.chunks(needed)
    symbol_rank = 0
    symbol_entities_by_chunk: dict[str, list[str]] = {}
    for entity_id, term, cls in symbols:
        chunk_id = symbol_chunk.get(entity_id)
        if chunk_id is None:
            continue
        row = candidates[chunk_id].chunk if chunk_id in candidates else chunk_rows.get(chunk_id)
        if row is None:
            continue
        symbol_entities_by_chunk.setdefault(chunk_id, []).append(entity_id)
        c = candidates.setdefault(chunk_id, _Candidate(row))
        if c.symbol_rank is None:
            symbol_rank += 1
            c.symbol_rank = symbol_rank
        c.origins.add("symbol")
        entity = entities[entity_id]
        c.why.append(
            f"symbol/path match for '{term}' ({('exact', 'prefix', 'substring', 'file path')[cls]}): "
            f"{entity.get('name')} [{entity.get('entity_type')}]"
        )
    for c in candidates.values():
        c.rrf = sum(1.0 / (limits.rrf_k + r) for r in (c.semantic_rank, c.symbol_rank) if r is not None)
    direct = sorted(
        candidates.values(),
        key=lambda c: (-c.rrf, min(r for r in (c.semantic_rank, c.symbol_rank) if r is not None), c.chunk["id"]),
    )
    for i, c in enumerate(direct):
        c.priority = (0, i)

    # 5. Seed entities -------------------------------------------------------------------
    links_by_chunk: dict[str, list[dict[str, Any]]] = {}
    for link in store.links("chunk_id", [c.chunk["id"] for c in direct]):
        links_by_chunk.setdefault(link["chunk_id"], []).append(link)
    seeds: list[str] = []
    for c in direct:
        preferred = symbol_entities_by_chunk.get(c.chunk["id"], [])
        others = [link["entity_id"] for link in links_by_chunk.get(c.chunk["id"], []) if link["entity_id"] not in preferred]
        for entity_id in (preferred + others)[:2]:
            if len(seeds) >= limits.seed_entities:
                break
            if entity_id not in seeds and entity_id in entities:
                seeds.append(entity_id)
    timings["fusion_and_seeds"] = ms(t)

    # 6. Bounded graph expansion ---------------------------------------------------------
    t = timer()
    relationships, discovered = _expand(store, entities, seeds, limits, truncated)
    timings["graph_expansion"] = ms(t)

    # 7. Excerpts of discovered entities -------------------------------------------------
    t = timer()
    hop_of = {entity_id: hop for entity_id, hop in discovered}
    graph_chunk = _best_chunk_per_entity(store.links("entity_id", [e for e, _hop in discovered]))
    new_ids = [cid for cid in dict.fromkeys(graph_chunk.values()) if cid not in candidates]
    fetched = store.chunks(new_ids)
    graph_candidates: list[_Candidate] = []
    for order, (entity_id, hop) in enumerate(discovered):
        chunk_id = graph_chunk.get(entity_id)
        if chunk_id is None:
            continue
        if chunk_id in candidates:
            candidates[chunk_id].origins.add("graph")
            continue
        row = fetched.get(chunk_id)
        if row is None:
            continue
        c = _Candidate(row, origins={"graph"}, priority=(1, hop, order))
        c.why.append(f"graph expansion: {entities[entity_id].get('name')} is {hop} hop(s) from a seed entity")
        candidates[chunk_id] = c
        graph_candidates.append(c)
    for link in store.links("chunk_id", [c.chunk["id"] for c in graph_candidates]):
        links_by_chunk.setdefault(link["chunk_id"], []).append(link)
    timings["evidence_fetch"] = ms(t)

    # 8. Merge and budget ----------------------------------------------------------------
    t = timer()
    rel_budget = int(limits.evidence_tokens * RELATIONSHIP_SHARE) if relationships else 0
    kept, rel_tokens = [], 0
    for rel in relationships:
        cost = estimate_tokens(render_relationship(rel))
        if rel_tokens + cost > rel_budget:
            continue
        kept.append(rel)
        rel_tokens += cost
    if len(kept) < len(relationships):
        truncated.append(f"{len(relationships) - len(kept)} relationships omitted by the context budget")
    evidence = _assemble(direct + graph_candidates, links_by_chunk, entities, limits.evidence_tokens - rel_tokens, truncated)
    timings["assemble"] = ms(t)

    limitations.extend(coverage_limitations(coverage or {}))
    if relationships:
        limitations.append(
            "Relationships come from static extraction: reachability does not prove a path executes at "
            "runtime, and dynamic dispatch, reflection or string-built hooks may be missing"
        )
    if not evidence:
        limitations.append("No source evidence matched this question in this job")
    kept, evidence = _renumber(kept, evidence)
    return Context(
        question=question,
        job_id=job_id,
        evidence=evidence,
        relationships=kept,
        limitations=limitations,
        terms=terms,
        semantic_used=semantic_used,
        truncated=truncated,
        timings_ms=timings,
    )


def _expand(
    store: _Store, entities: dict[str, dict[str, Any]], seeds: list[str], limits: Limits, truncated: list[str]
) -> tuple[list[Relationship], list[tuple[str, int]]]:
    visited: dict[str, int] = {s: 0 for s in seeds}
    relationships: dict[str, Relationship] = {}
    discovered: list[tuple[str, int]] = []
    frontier = list(seeds)
    for hop in range(1, limits.hops + 1):
        if not frontier or len(relationships) >= limits.max_edges:
            break
        rows = store.edges("from_entity_id", frontier) + store.edges("to_entity_id", frontier)
        per_node: dict[str, int] = {}
        next_frontier: list[str] = []
        for row in sorted(rows, key=lambda r: r["id"]):
            src, dst = row["from_entity_id"], row.get("to_entity_id")
            if dst is None or src not in entities or dst not in entities or row["id"] in relationships:
                continue
            via = src if src in frontier else dst
            per_node[via] = per_node.get(via, 0) + 1
            if per_node[via] > limits.per_node_edges:
                if per_node[via] == limits.per_node_edges + 1:
                    truncated.append(f"{entities[via].get('name')} has more than {limits.per_node_edges} relationships; expanded {limits.per_node_edges}")
                continue
            if len(relationships) >= limits.max_edges:
                truncated.append(f"relationship limit {limits.max_edges} reached at hop {hop}")
                break
            other = dst if via == src else src
            if other not in visited:
                if len(discovered) >= limits.max_expanded_nodes:
                    continue
                visited[other] = hop
                discovered.append((other, hop))
                next_frontier.append(other)
            relationships[row["id"]] = Relationship(
                relationship_id="",
                edge_id=row["id"],
                edge_type=row["edge_type"],
                source=_ref(entities[src]),
                target=_ref(entities[dst]),
                provenance=row.get("provenance") or {},
                hop=hop,
            )
        frontier = next_frontier
    return list(relationships.values()), discovered


def _assemble(
    candidates: list[_Candidate],
    links_by_chunk: dict[str, list[dict[str, Any]]],
    entities: dict[str, dict[str, Any]],
    budget: int,
    truncated: list[str],
) -> list[Evidence]:
    """Merge overlapping/adjacent excerpts of one file, then admit groups in priority order
    while they fit the token budget."""
    groups: list[dict[str, Any]] = []
    for c in sorted(candidates, key=lambda c: c.priority):
        ch = c.chunk
        target = next(
            (
                g
                for g in groups
                if g["file_path"] == ch["file_path"]
                and not (ch["line_start"] > g["line_end"] + 1 or ch["line_end"] < g["line_start"] - 1)
            ),
            None,
        )
        if target is None:
            target = {"file_path": ch["file_path"], "line_start": ch["line_start"], "line_end": ch["line_end"],
                      "lines": {}, "members": [], "priority": c.priority}
            groups.append(target)
        target["line_start"] = min(target["line_start"], ch["line_start"])
        target["line_end"] = max(target["line_end"], ch["line_end"])
        for offset, text in enumerate(ch["content"].split("\n")):
            target["lines"].setdefault(ch["line_start"] + offset, text)
        target["members"].append(c)
    groups.sort(key=lambda g: g["priority"])
    items: list[Evidence] = []
    used = dropped = 0
    for g in groups:
        content = "\n".join(g["lines"].get(n, "") for n in range(g["line_start"], g["line_end"] + 1))
        cost = estimate_tokens(content) + 30
        if used + cost > budget:
            dropped += 1
            continue
        used += cost
        members: list[_Candidate] = g["members"]
        chunk_ids = sorted(m.chunk["id"] for m in members)
        refs: dict[str, EntityRef] = {}
        for m in members:
            for link in links_by_chunk.get(m.chunk["id"], []):
                if link["entity_id"] in entities and link["entity_id"] not in refs:
                    refs[link["entity_id"]] = _ref(entities[link["entity_id"]], link["association"])
        items.append(
            Evidence(
                evidence_id="",
                key="ev" + hashlib.sha256("|".join(chunk_ids).encode()).hexdigest()[:20],
                file_path=g["file_path"],
                line_start=g["line_start"],
                line_end=g["line_end"],
                content=content,
                origins=sorted({o for m in members for o in m.origins}),
                why=[w for m in members for w in m.why],
                entities=list(refs.values())[:30],
                chunk_ids=chunk_ids,
            )
        )
    if dropped:
        truncated.append(f"{dropped} evidence excerpts omitted by the {budget}-token evidence budget")
    return items


def coverage_limitations(coverage: dict[str, Any]) -> list[str]:
    """Honest statements about what this job's evidence can't contain."""
    out: list[str] = []
    graphify = coverage.get("graphify") or {}
    status = graphify.get("status")
    if status == "unavailable":
        out.append("Graphify was unavailable for this job, so only PHP files were analysed")
    elif status == "ran" and graphify.get("not_extracted_count"):
        out.append(f"{graphify['not_extracted_count']} files were only partially extracted or not extracted by Graphify")
    if graphify.get("files_oversized"):
        out.append(f"{graphify['files_oversized']} files were too large to analyse")
    evidence = coverage.get("evidence") or {}
    if evidence.get("files_skipped_count"):
        out.append(f"{evidence['files_skipped_count']} files have no stored excerpts (unreadable, binary or oversized)")
    if (evidence.get("entity_range_basis") or {}).get("start_line_only"):
        out.append(
            f"{evidence['entity_range_basis']['start_line_only']} entities have only a start line; their excerpts "
            "are source windows, not verified complete definitions"
        )
    if evidence.get("truncated_at_max_chunks"):
        out.append("Evidence storage stopped at its chunk limit; later files have no excerpts")
    return out


def render_relationship(rel: Relationship) -> str:
    def ent(e: EntityRef) -> str:
        loc = f"{e.file_path}:{e.line}" if e.line else e.file_path
        return f"{e.name} ({e.entity_type}, {loc})"

    prov = rel.provenance or {}
    confidence = prov.get("confidence") or "EXTRACTED"
    if isinstance(prov.get("confidence_score"), int | float):
        confidence += f" {prov['confidence_score']:.2f}"
    inferred = ", inferred" if confidence.upper().split()[0] != "EXTRACTED" else ""
    where = f" at {prov['file_path']}:{prov['line']}" if prov.get("file_path") and prov.get("line") else ""
    via = f" via {prov['extractor']}" if prov.get("extractor") else ""
    return f"{rel.relationship_id}: {ent(rel.source)} --{rel.edge_type} [{confidence}{inferred}{via}]--> {ent(rel.target)}{where}"


def _renumber(rels: list[Relationship], evidence: list[Evidence]) -> tuple[list[Relationship], list[Evidence]]:
    for i, r in enumerate(rels, start=1):
        r.relationship_id = f"R{i}"
    for i, e in enumerate(evidence, start=1):
        e.evidence_id = f"E{i}"
    return rels, evidence
