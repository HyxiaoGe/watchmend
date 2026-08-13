from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

from sentinel.codex_reset.models import FetchedSource, ResetEvidence, ResetStage, ResetType

_OFFICIAL_HANDLES = {"thsottiaux", "openai", "sama", "romainhuet", "gdb"}
_STATUS_ID = re.compile(r"/(?:i/web/)?status/(\d+)", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\"']+")
_COMPLETED = re.compile(
    r"(?:limits?|quota).{0,30}(?:have been |has been |were |was )reset"
    r"|(?:reset|重置).{0,20}(?:is |was )?(?:done|complete|completed|已完成|完成|落地)"
    r"|(?:done|complete|completed).{0,30}(?:the )?(?:reset|重置)"
    r"|(?:已|已经).{0,8}重置",
    re.IGNORECASE,
)
_FUTURE_RESET = re.compile(
    r"(?:reset|重置).{0,50}(?:tomorrow|monday|next|soon|later|within|未来|明天|稍后|小时)"
    r"|(?:tomorrow|monday|next|soon|later|within|未来|明天|稍后|小时).{0,50}(?:reset|重置)",
    re.IGNORECASE,
)
_DURATION = re.compile(r"(\d+)\s*(?:hours?|小时)", re.IGNORECASE)


def parse_timestamp(value: object) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = (
            parsedate_to_datetime(raw)
            if "," in raw
            else datetime.fromisoformat(raw.replace("Z", "+00:00"))
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def extract_status_id(url: str) -> str | None:
    match = _STATUS_ID.search(url)
    return match.group(1) if match else None


def canonical_from(url: str, item_id: str) -> str:
    status_id = extract_status_id(url)
    if status_id:
        return f"x:{status_id}"
    clean = item_id.strip() or hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return f"public:{clean}"


def is_official_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return False
    parts = [part.lower() for part in parsed.path.split("/") if part]
    return bool(parts and parts[0] in _OFFICIAL_HANDLES)


def infer_reset_type(value: object, text: str) -> ResetType | None:
    raw = f"{value or ''} {text}".lower()
    if "banked" in raw or "储备" in raw or "入账" in raw:
        return ResetType.BANKED
    if any(word in raw for word in ("hard", "direct", "reset", "重置")):
        return ResetType.DIRECT
    return None


def _timeline_evidence(
    event: dict, *, source_name: str, source_family: str
) -> ResetEvidence | None:
    if event.get("type") != "reset" and event.get("group") != "reset":
        return None
    item_id = str(event.get("id") or "").strip()
    url = str(event.get("url") or "").strip()
    summary = str(event.get("summary") or "").strip()
    observed_at = parse_timestamp(event.get("effective_at") or event.get("announced_at"))
    if not item_id or not url or not summary or observed_at is None:
        return None
    official = is_official_url(url)
    reset_type = infer_reset_type(event.get("reset_kind"), summary)
    window = event.get("official_window") if isinstance(event.get("official_window"), dict) else {}
    expected_start = parse_timestamp(window.get("start_at"))
    expected_end = parse_timestamp(window.get("end_at"))
    completed = bool(_COMPLETED.search(summary))
    state = str(event.get("announcement_state") or "").lower()
    verified = str(event.get("reset_verification_status") or "").lower() == "confirmed"
    observed = str(event.get("observation_result") or "").lower() in {
        "confirmed",
        "reset_observed",
    }
    archived = (
        event.get("source") == "archive"
        and event.get("confidence") == "high"
        and reset_type is not None
    )
    if completed and (official or archived or verified or observed):
        stage = ResetStage.CONFIRMED
    elif official and state == "announced" and expected_end is not None and reset_type is not None:
        stage = ResetStage.ANNOUNCED
    elif official and (state == "hinted" or _FUTURE_RESET.search(summary)):
        stage = ResetStage.HINT
    else:
        return None
    return ResetEvidence(
        source_name=source_name,
        source_family=source_family,
        source_item_id=item_id,
        canonical_hint=canonical_from(url, item_id),
        signal_stage=stage,
        title=str(event.get("source_label") or "Codex reset signal"),
        summary=summary,
        url=url,
        observed_at=observed_at,
        reset_type=reset_type,
        expected_start_ts=expected_start,
        expected_end_ts=expected_end,
        official=official,
        explicit_completed=completed,
    )


def parse_timeline(
    data: object,
    *,
    source_name: str = "reset_timeline",
    source_family: str = "codexreset",
) -> FetchedSource:
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("reset timeline must contain an events array")
    evidence = [
        item
        for event in data["events"]
        if isinstance(event, dict)
        and (
            item := _timeline_evidence(event, source_name=source_name, source_family=source_family)
        )
        is not None
    ]
    return FetchedSource(
        name=source_name,
        family=source_family,
        content_ts=parse_timestamp(data.get("updated_at") or data.get("fetched_at")),
        evidence=evidence,
    )


def parse_reset_feed(data: object) -> FetchedSource:
    if not isinstance(data, dict):
        raise ValueError("reset feed must be an object")
    parsed = parse_timeline(data, source_name="reset_feed", source_family="codexreset")
    by_item = {item.source_item_id: item for item in parsed.evidence}
    tweets = data.get("tweets") if isinstance(data.get("tweets"), list) else []
    for tweet in tweets:
        if not isinstance(tweet, dict):
            continue
        verification = tweet.get("reset_verification")
        verification = verification if isinstance(verification, dict) else {}
        status = str(tweet.get("reset_verification_status") or verification.get("status") or "")
        summary = str(tweet.get("text") or "").strip()
        url = str(tweet.get("url") or "").strip()
        item_id = str(tweet.get("id") or "").strip()
        observed_at = parse_timestamp(
            verification.get("observed_at") or tweet.get("declared_at") or tweet.get("at")
        )
        contextual_id = str(tweet.get("contextual_reset_source_id") or "").strip()
        if contextual_id and item_id in by_item:
            by_item[item_id] = replace(
                by_item[item_id],
                canonical_hint=f"x:{contextual_id}",
            )
        if status != "confirmed" or not summary or not url or not item_id or observed_at is None:
            continue
        canonical_hint = (
            by_item[item_id].canonical_hint
            if item_id in by_item
            else f"x:{contextual_id}"
            if contextual_id
            else canonical_from(url, item_id)
        )
        by_item[item_id] = ResetEvidence(
            source_name="reset_feed",
            source_family="codexreset",
            source_item_id=item_id,
            canonical_hint=canonical_hint,
            signal_stage=ResetStage.CONFIRMED,
            title="Codex reset verified by public feed",
            summary=summary,
            url=url,
            observed_at=observed_at,
            reset_type=infer_reset_type("hard", summary),
            official=is_official_url(url),
            explicit_completed=True,
            # 公开 feed 报告的 reference 不是本机参考账号，不能当作本机只读证据。
            local_reference=False,
        )
    return FetchedSource(
        name="reset_feed",
        family="codexreset",
        content_ts=parse_timestamp(data.get("fetched_at")),
        evidence=list(by_item.values()),
    )


def parse_radar_current(data: object) -> FetchedSource:
    if not isinstance(data, dict) or data.get("schema_version") != "2.0":
        raise ValueError("unsupported radar current schema")
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    evidence: list[ResetEvidence] = []
    url = str(window.get("source_url") or "").strip()
    opened_at = parse_timestamp(window.get("opened_at"))
    if window.get("open") is True and url and opened_at is not None and is_official_url(url):
        item_id = extract_status_id(url) or hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        title = str(window.get("title") or "Codex reset radar signal")
        summary = " · ".join(
            part
            for part in (title, str(window.get("message") or ""), str(window.get("scope") or ""))
            if part
        )
        evidence.append(
            ResetEvidence(
                source_name="radar_current",
                source_family="codexradar",
                source_item_id=item_id,
                canonical_hint=canonical_from(url, item_id),
                signal_stage=ResetStage.HINT,
                title=title,
                summary=summary,
                url=url,
                observed_at=opened_at,
                reset_type=infer_reset_type(None, summary),
                official=True,
            )
        )
    return FetchedSource(
        name="radar_current",
        family="codexradar",
        content_ts=parse_timestamp(data.get("monitored_at")),
        evidence=evidence,
    )


def _description_source(description: str) -> str:
    for match in _URL.findall(description):
        if "x.com/" in match or "twitter.com/" in match:
            return match.rstrip(".,，。")
    return ""


def parse_radar_rss(text: str) -> FetchedSource:
    root = ET.fromstring(text)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("RSS channel missing")
    content_ts = parse_timestamp(channel.findtext("lastBuildDate"))
    evidence: list[ResetEvidence] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        description = (item.findtext("description") or "").strip()
        item_id = (item.findtext("guid") or "").strip()
        observed_at = parse_timestamp(item.findtext("pubDate"))
        url = _description_source(description)
        if not title or not item_id or observed_at is None or not url:
            continue
        official = is_official_url(url)
        reset_type = infer_reset_type(None, f"{title} {description}")
        completed = bool(
            _COMPLETED.search(f"{title} {description}")
            or any(word in title for word in ("关闭", "已重置", "完成"))
        )
        duration = _DURATION.search(f"{title} {description}")
        expected_end = observed_at + int(duration.group(1)) * 3600 if duration else None
        if completed:
            stage = ResetStage.CONFIRMED
        elif official and "开启" in title and expected_end is not None and reset_type is not None:
            stage = ResetStage.ANNOUNCED
        elif official and ("开启" in title or _FUTURE_RESET.search(description)):
            stage = ResetStage.HINT
        else:
            continue
        evidence.append(
            ResetEvidence(
                source_name="radar_rss",
                source_family="codexradar",
                source_item_id=item_id,
                canonical_hint=canonical_from(url, item_id),
                signal_stage=stage,
                title=title,
                summary=description[:2000],
                url=url,
                observed_at=observed_at,
                reset_type=reset_type,
                expected_start_ts=observed_at if expected_end is not None else None,
                expected_end_ts=expected_end,
                official=official,
                explicit_completed=completed,
            )
        )
    return FetchedSource(
        name="radar_rss", family="codexradar", content_ts=content_ts, evidence=evidence
    )


class _ResetTimelineHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._current: dict | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if self._current is None and values.get("data-testid") == "reset-timeline-item":
            self._current = {
                "kind": values.get("data-kind") or "",
                "url": values.get("data-source-url") or "",
                "datetime": values.get("data-datetime") or "",
                "text": [],
            }
            self._depth = 1
        elif self._current is not None:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        self._depth -= 1
        if self._depth == 0:
            self.items.append(self._current)
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is not None and data.strip():
            self._current["text"].append(data.strip())


def parse_reset_html(text: str) -> FetchedSource:
    parser = _ResetTimelineHTMLParser()
    parser.feed(text)
    evidence: list[ResetEvidence] = []
    content_ts: int | None = None
    for item in parser.items:
        observed_at = parse_timestamp(item["datetime"])
        if observed_at is None:
            continue
        content_ts = max(content_ts or observed_at, observed_at)
        summary = " ".join(item["text"])
        url = item["url"]
        if item["kind"] != "confirmed" or not re.search(r"reset|quota|重置", summary, re.I):
            continue
        item_id = extract_status_id(url) or hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        evidence.append(
            ResetEvidence(
                source_name="reset_html",
                source_family="codexreset_org",
                source_item_id=item_id,
                canonical_hint=canonical_from(url, item_id),
                signal_stage=ResetStage.CONFIRMED,
                title="Codex reset HTML cross-check",
                summary=summary[:2000],
                url=url,
                observed_at=observed_at,
                reset_type=infer_reset_type("hard", summary),
                official=is_official_url(url),
                explicit_completed=True,
            )
        )
    return FetchedSource(
        name="reset_html",
        family="codexreset_org",
        content_ts=content_ts,
        evidence=evidence,
    )


class PublicResetSource:
    def __init__(
        self,
        *,
        name: str,
        family: str,
        url: str,
        parser: Callable,
        json_response: bool,
    ) -> None:
        self.name = name
        self.family = family
        self.url = url
        self._parser = parser
        self._json_response = json_response

    async def fetch(self, fetcher) -> FetchedSource:
        payload = (
            await fetcher.get_json(self.url)
            if self._json_response
            else await fetcher.get_text(self.url)
        )
        return self._parser(payload)


def default_sources(settings) -> list[PublicResetSource]:
    return [
        PublicResetSource(
            name="radar_current",
            family="codexradar",
            url=settings.sentinel_codex_reset_radar_current_url,
            parser=parse_radar_current,
            json_response=True,
        ),
        PublicResetSource(
            name="radar_rss",
            family="codexradar",
            url=settings.sentinel_codex_reset_rss_url,
            parser=parse_radar_rss,
            json_response=False,
        ),
        PublicResetSource(
            name="reset_feed",
            family="codexreset",
            url=settings.sentinel_codex_reset_feed_url,
            parser=parse_reset_feed,
            json_response=True,
        ),
        PublicResetSource(
            name="reset_timeline",
            family="codexreset",
            url=settings.sentinel_codex_reset_timeline_url,
            parser=parse_timeline,
            json_response=True,
        ),
        PublicResetSource(
            name="reset_html",
            family="codexreset_org",
            url=settings.sentinel_codex_reset_html_url,
            parser=parse_reset_html,
            json_response=False,
        ),
    ]
