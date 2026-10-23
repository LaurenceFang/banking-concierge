"""Small, local-only Markdown retrieval service for the first usable slice.

The manifest is the source of document metadata and validity. Seed documents
are project-authored simulation guidance; ``draft`` is intentionally searchable
so the demo can answer from its checked-in seed content. Explicitly retired,
superseded, or otherwise inactive documents are excluded.

Retrieval uses BM25 over heading-aware semantic chunks. Jieba is used when the
application installs it; the built-in fallback uses CJK bigrams plus Latin and
numeric terms, so local retrieval still works in a minimal Python environment.
No model, embedding endpoint, or external service is called here.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:  # The parent application may declare jieba as an optional dependency.
    import jieba  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on the host environment
    jieba = None


_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_LATIN_OR_NUMBER_RE = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", re.IGNORECASE)
_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_INACTIVE_STATUSES = {
    "archived",
    "disabled",
    "expired",
    "inactive",
    "retired",
    "superseded",
    "withdrawn",
}
_SEARCHABLE_STATUSES = {"active", "current", "draft", "published"}
_MAX_CHUNK_CHARS = 600

# These aliases are deliberately small and tied to the demo's user vocabulary.
# They add a shared retrieval token without rewriting the source text.
_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("手续费", "服务费", "费用", "收费"),
    ("代扣", "自动扣款", "自动扣费", "停止扣款"),
    ("退款", "退回", "退钱"),
    ("余额", "账户余额", "可用余额", "账面余额", "剩余资金"),
    ("挂失", "报失", "丢卡"),
    ("冻结", "锁卡", "临时锁卡"),
    ("调额", "额度调整", "调整额度"),
    ("异常交易", "可疑交易", "异常消费"),
    ("商户", "商家"),
    ("取消会员", "会员续费", "取消订阅", "取消自动续费"),
    ("订阅候选", "候选订阅", "误判", "错误识别", "不是订阅"),
)

# A small router for follow-up retrieval, not an intent classifier. Explicit
# domain words prevent a question about a new topic from inheriting stale
# history. Generic terms such as "费用" are intentionally absent because they
# may apply to transfers, subscriptions, wealth products, or billing.
_CONTEXT_DOMAINS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "wealth",
        ("理财", "申购", "赎回", "持仓", "份额", "风险测评", "锁定期", "结算"),
    ),
    (
        "cards",
        ("信用卡", "借记卡", "银行卡", "卡片", "卡号", "挂失", "冻结", "解锁", "调额", "额度"),
    ),
    (
        "subscriptions",
        ("订阅", "会员", "续费", "自动续费", "代扣", "扣款授权", "云服务"),
    ),
    (
        "billing",
        ("账单", "流水", "csv", "导入", "分类", "记账", "报表", "月报", "收支"),
    ),
    (
        "tasks",
        ("aa", "均摊", "分摊", "定时转账", "定时", "付款请求", "待收款"),
    ),
    (
        "workflow",
        ("生活安排", "生日", "鲜花", "花束", "蛋糕", "服务目录", "安排商户"),
    ),
    (
        "transfer",
        ("转账", "转给", "转出", "收款人", "收付款", "收款账户", "收款", "转款"),
    ),
)
_FOLLOWUP_RE = re.compile(
    r"(?:^|\s)(?:那|那么|这个|那笔|这笔|它|刚才|上面|上述|接着|继续|之后)|"
    r"(?:呢|吗|么|多少|多久|什么时候|怎么|如何|能不能|可不可以|是否|具体|会不会)[？?！!。…]*$"
)
_TOPIC_SWITCH_RE = re.compile(r"(?:换个话题|另一个问题|另外一件事|现在问另一个|说个新的|不说这个了)")
_CONTEXT_HISTORY_WINDOW = 6
_MAX_CONTEXT_CHARS = 120


@dataclass(frozen=True)
class _Document:
    doc_id: str
    markdown_title: str
    version: str
    category: str
    source: str | None
    scope: str | None
    valid_from: date | None
    valid_to: date | None


@dataclass(frozen=True)
class _Chunk:
    document: _Document
    headings: tuple[str, ...]
    text: str
    indexed_text: str
    tokens: tuple[str, ...]

    @property
    def title(self) -> str:
        if not self.headings:
            return self.document.markdown_title
        return f"{self.document.markdown_title} · {' / '.join(self.headings)}"


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _context_topics(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    topics: set[str] = set()
    for topic, terms in _CONTEXT_DOMAINS:
        for term in terms:
            if term == "aa":
                if re.search(r"(?<![a-z0-9])aa(?![a-z0-9])", normalized):
                    topics.add(topic)
                    break
            elif term in normalized:
                topics.add(topic)
                break
    return topics


def _context_for_followup(query: str, history: list[str] | None) -> str | None:
    """Select one recent user utterance for a short, plausibly referential query."""
    normalized_query = unicodedata.normalize("NFKC", query).strip()
    if (
        not normalized_query
        or len(normalized_query) > 48
        or _TOPIC_SWITCH_RE.search(normalized_query)
        or not _FOLLOWUP_RE.search(normalized_query)
    ):
        return None

    recent_messages = [
        unicodedata.normalize("NFKC", item).strip()
        for item in (history or [])[-_CONTEXT_HISTORY_WINDOW:]
        if isinstance(item, str) and item.strip()
    ]
    if not recent_messages:
        return None

    current_topics = _context_topics(normalized_query)
    previous = next(
        (
            message
            for message in reversed(recent_messages)
            if _context_topics(message)
        ),
        None,
    )
    if previous is None:
        return None

    previous_topics = _context_topics(previous)
    # Every explicit domain in the current question must already be present in
    # the selected context. A shared word such as "转账" must not pull a mixed
    # question about a newly named card topic back into an older transfer turn.
    if current_topics and not current_topics.issubset(previous_topics):
        return None

    context = re.sub(r"\s+", " ", previous).strip()
    return context[-_MAX_CONTEXT_CHARS:] or None


def _parse_date(value: Any, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    try:
        if "T" in raw or " " in raw:
            return datetime.fromisoformat(raw).date()
        return date.fromisoformat(raw[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid manifest date for {field_name}: {raw!r}") from exc


def _current_entry(entry: dict[str, Any], today: date) -> bool:
    status = _as_text(entry.get("status"), "draft").casefold()
    if status in _INACTIVE_STATUSES:
        return False
    if status not in _SEARCHABLE_STATUSES:
        return False

    valid_from = _parse_date(
        entry.get("valid_from", entry.get("effective_from")), "valid_from"
    )
    valid_to = _parse_date(
        entry.get("valid_to", entry.get("valid_until", entry.get("effective_to"))),
        "valid_to",
    )
    expires_at = _parse_date(entry.get("expires_at"), "expires_at")
    if valid_from is not None and valid_from > today:
        return False
    for end_date in (valid_to, expires_at):
        if end_date is not None and end_date < today:
            return False
    return True


def _fallback_tokens(text: str) -> list[str]:
    """Tokenize Chinese phrases consistently without a third-party package."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    for match in re.finditer(
        rf"({_CJK_RUN_RE.pattern})|({_LATIN_OR_NUMBER_RE.pattern})", normalized
    ):
        cjk_run, latin = match.groups()
        if cjk_run:
            if len(cjk_run) == 1:
                tokens.append(cjk_run)
            else:
                # Unigrams make almost any Chinese question overlap with any
                # document. Bigrams retain useful phrase matching without
                # flooding results with incidental shared characters.
                tokens.extend(cjk_run[i : i + 2] for i in range(len(cjk_run) - 1))
        elif latin:
            tokens.append(latin)

    for group_index, group in enumerate(_SYNONYM_GROUPS):
        if any(alias in normalized for alias in group):
            tokens.append(f"__synonym_{group_index}__")
    return tokens


def _tokenize(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    if jieba is None:
        return tuple(_fallback_tokens(normalized))

    tokens = []
    for raw_token in jieba.cut_for_search(normalized):
        token = raw_token.strip().casefold()
        if not token:
            continue
        if len(token) == 1 and _CJK_RUN_RE.fullmatch(token) and len(normalized) > 1:
            continue
        tokens.append(token)
    # Preserve identifiers and numbers even if a segmenter treats punctuation
    # around them as a separate token.
    tokens.extend(match.group(0).casefold() for match in _LATIN_OR_NUMBER_RE.finditer(normalized))
    for group_index, group in enumerate(_SYNONYM_GROUPS):
        if any(alias in normalized for alias in group):
            tokens.append(f"__synonym_{group_index}__")
    return tuple(tokens)


def _split_long_text(text: str, max_chars: int) -> list[str]:
    """Split oversized paragraphs at sentence punctuation where possible."""
    if len(text) <= max_chars:
        return [text]
    sentences = re.split(r"(?<=[。！？!?；;])", text)
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            for start in range(0, len(sentence), max_chars):
                pieces.append(sentence[start : start + max_chars])
            continue
        if current and len(current) + len(sentence) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        pieces.append(current)
    return pieces


def _pack_paragraphs(paragraphs: Iterable[str], max_chars: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        for piece in _split_long_text(paragraph.strip(), max_chars):
            if not piece:
                continue
            candidate = f"{current}\n\n{piece}" if current else piece
            if current and len(candidate) > max_chars:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def _markdown_sections(markdown: str) -> tuple[str, list[tuple[tuple[str, ...], list[str]]]]:
    """Return the H1 title and text groups keyed by their heading hierarchy."""
    title = ""
    heading_stack: dict[int, str] = {}
    sections: list[tuple[tuple[str, ...], list[str]]] = []
    paragraph_lines: list[str] = []
    section_paragraphs: list[str] = []
    section_headings: tuple[str, ...] = ()

    def flush_paragraph() -> None:
        if paragraph_lines:
            paragraph = " ".join(part.strip() for part in paragraph_lines).strip()
            if paragraph:
                section_paragraphs.append(paragraph)
            paragraph_lines.clear()

    def flush_section() -> None:
        nonlocal section_paragraphs
        flush_paragraph()
        if section_paragraphs:
            sections.append((section_headings, section_paragraphs))
            section_paragraphs = []

    for line in markdown.splitlines():
        heading_match = _MARKDOWN_HEADING_RE.match(line)
        if heading_match:
            flush_section()
            level = len(heading_match.group(1))
            heading = heading_match.group(2).strip()
            if level == 1 and not title:
                title = heading
                heading_stack.clear()
                section_headings = ()
                continue
            if level == 1:
                title = heading
                heading_stack.clear()
            else:
                heading_stack = {
                    heading_level: existing_heading
                    for heading_level, existing_heading in heading_stack.items()
                    if heading_level < level
                }
                heading_stack[level] = heading
            section_headings = tuple(
                heading_stack[heading_level]
                for heading_level in sorted(heading_stack)
            )
            continue
        if not line.strip():
            flush_paragraph()
        else:
            paragraph_lines.append(line)

    flush_section()
    if not title:
        title = ""
    return title, sections


class KnowledgeService:
    """Load current Markdown documents and rank their semantic chunks with BM25."""

    def __init__(self, knowledge_dir: str | Path):
        self.knowledge_dir = Path(knowledge_dir).expanduser().resolve()
        self.manifest_path = self.knowledge_dir / "manifest.json"
        self._documents: tuple[_Document, ...] = ()
        self._chunks: tuple[_Chunk, ...] = ()
        self._document_frequency: Counter[str] = Counter()
        self._average_length = 0.0
        self.reload()

    @property
    def document_count(self) -> int:
        """Number of current manifest documents successfully loaded."""
        return len(self._documents)

    def reload(self) -> int:
        """Reload the manifest and Markdown files, replacing the index atomically."""
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Knowledge manifest not found: {self.manifest_path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid knowledge manifest JSON: {self.manifest_path}") from exc

        entries = manifest.get("documents") if isinstance(manifest, dict) else None
        if not isinstance(entries, list):
            raise TypeError("Knowledge manifest must contain a documents array")

        today = date.today()
        base_dir = self.knowledge_dir
        loaded_documents: list[_Document] = []
        loaded_chunks: list[_Chunk] = []
        seen_ids: set[str] = set()

        for entry in entries:
            if not isinstance(entry, dict) or not _current_entry(entry, today):
                continue

            doc_id = _as_text(entry.get("doc_id"))
            relative_path = _as_text(entry.get("path"))
            if not doc_id or not relative_path:
                continue
            if doc_id in seen_ids:
                raise ValueError(f"Duplicate document id in knowledge manifest: {doc_id}")
            seen_ids.add(doc_id)

            source_path = (base_dir / relative_path).resolve()
            try:
                source_path.relative_to(base_dir)
            except ValueError as exc:
                raise ValueError(f"Knowledge document path escapes its directory: {relative_path}") from exc
            if not source_path.is_file():
                continue

            markdown = source_path.read_text(encoding="utf-8")
            markdown_title, sections = _markdown_sections(markdown)
            document = _Document(
                doc_id=doc_id,
                markdown_title=markdown_title or _as_text(entry.get("title"), doc_id),
                version=_as_text(entry.get("version"), ""),
                category=_as_text(entry.get("category"), "uncategorized"),
                source=_as_text(entry.get("source"), "") or None,
                scope=_as_text(entry.get("scope"), "") or None,
                valid_from=_parse_date(
                    entry.get("valid_from", entry.get("effective_from")), "valid_from"
                ),
                valid_to=_parse_date(
                    entry.get("valid_to", entry.get("valid_until", entry.get("effective_to"))),
                    "valid_to",
                ),
            )

            document_chunks = 0
            for headings, paragraphs in sections:
                for content in _pack_paragraphs(paragraphs, _MAX_CHUNK_CHARS):
                    heading_text = " / ".join(headings)
                    indexed_text = " ".join(part for part in (heading_text, content) if part)
                    tokens = _tokenize(indexed_text)
                    if not tokens:
                        continue
                    loaded_chunks.append(
                        _Chunk(
                            document=document,
                            headings=headings,
                            text=content,
                            indexed_text=indexed_text,
                            tokens=tokens,
                        )
                    )
                    document_chunks += 1
            if document_chunks:
                loaded_documents.append(document)

        document_frequency: Counter[str] = Counter()
        total_length = 0
        for chunk in loaded_chunks:
            document_frequency.update(set(chunk.tokens))
            total_length += len(chunk.tokens)

        # Publish only after every referenced document has been processed.
        self._documents = tuple(loaded_documents)
        self._chunks = tuple(loaded_chunks)
        self._document_frequency = document_frequency
        self._average_length = total_length / len(loaded_chunks) if loaded_chunks else 0.0
        return self.document_count

    def search(
        self, query: str, limit: int = 5, category: str | None = None
    ) -> list[dict[str, str | float]]:
        """Return the highest scoring current chunks for a Chinese or mixed query."""
        if limit <= 0 or not query or not query.strip():
            return []
        query_tokens = set(_tokenize(query))
        if not query_tokens:
            return []

        candidates = [
            chunk
            for chunk in self._chunks
            if category is None or chunk.document.category == category
        ]
        total_chunks = len(candidates)
        if not total_chunks:
            return []
        average_length = sum(len(chunk.tokens) for chunk in candidates) / total_chunks
        if average_length <= 0:
            return []

        # Recompute filtered document frequencies when the caller supplies a
        # category so scores compare only against that category's corpus.
        if category is None:
            document_frequency = self._document_frequency
        else:
            document_frequency = Counter()
            for chunk in candidates:
                document_frequency.update(set(chunk.tokens))

        normalized_query = unicodedata.normalize("NFKC", query).casefold().strip()
        ranked: list[tuple[float, int, _Chunk]] = []
        k1 = 1.5
        b = 0.75
        for order, chunk in enumerate(candidates):
            frequencies = Counter(chunk.tokens)
            length = len(chunk.tokens)
            score = 0.0
            for token in query_tokens:
                frequency = frequencies.get(token, 0)
                if frequency == 0:
                    continue
                df = document_frequency[token]
                inverse_frequency = math.log1p(
                    (total_chunks - df + 0.5) / (df + 0.5)
                )
                denominator = frequency + k1 * (1 - b + b * length / average_length)
                score += inverse_frequency * frequency * (k1 + 1) / denominator
            if score <= 0:
                continue
            if normalized_query and normalized_query in chunk.indexed_text.casefold():
                score += 1.0
            ranked.append((score, order, chunk))

        ranked.sort(key=lambda item: (-item[0], item[2].document.doc_id, item[1]))
        return [
            {
                "doc_id": chunk.document.doc_id,
                "title": chunk.title,
                "excerpt": (
                    f"{' / '.join(chunk.headings)}\n{chunk.text}"
                    if chunk.headings
                    else chunk.text
                ),
                "version": chunk.document.version,
                "score": round(score, 6),
            }
            for score, _order, chunk in ranked[:limit]
        ]

    def search_contextual(
        self,
        query: str,
        history: list[str] | None = None,
        limit: int = 4,
    ) -> list[dict[str, str | float]]:
        """Retrieve with bounded user-history expansion for short follow-ups.

        ``history`` contains recent user messages and excludes the current query.
        The BM25 ``search`` method remains the baseline and is used unchanged
        whenever the query is self-contained, has an explicit topic switch, or
        names a different domain from the most recent relevant user message.
        The return rows intentionally keep the exact ``search`` shape.
        """
        if limit <= 0 or not isinstance(query, str) or not query.strip():
            return []
        context = _context_for_followup(query, history)
        if context is None:
            return self.search(query, limit=limit)
        return self.search(f"{query.strip()} {context}", limit=limit)
