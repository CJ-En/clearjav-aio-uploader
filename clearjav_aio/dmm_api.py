"""DVD id and release date from DMM's own product API, for ids r18.dev has no DVD id for.

It asks ppvContent for makerContentId and deliveryStartDate. DMM's API answers it with the tool's
own name; the content id is used exactly as given.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from . import __version__

log = logging.getLogger(__name__)

URL = "https://api.video.dmm.co.jp/graphql"
# Title, maker and length (seconds) show the member which video a suggested content id is.
QUERY = "query GetContent($id: ID!) { ppvContent(id: $id) { id makerContentId deliveryStartDate title duration maker { name } } }"
# DMM's digital listing of another listing's id (ipzz020 -> ipzz00020, checked 2026-10-15). DMM's API has
# no search by DVD id (its queryWord searches titles and names).
DIGITAL_QUERY = "query GetDigitalId($id: ID!) { ppvContentSummaryByExternalId(externalId: $id) { id } }"
JAPAN = timezone(timedelta(hours=9))


@dataclass
class Product:
    content_id: str
    dvd_id: str
    release_date: str  # YYYY-MM-DD in Japan, like DMM's pages
    title: str = ""  # Japanese
    maker: str = ""
    runtime: int = 0  # minutes


def japan_date(value: str) -> str:
    """DMM's delivery start is UTC ("2026-01-24T15:00:00Z"); its pages show the date in Japan."""
    text = str(value or "").strip()
    if "T" not in text:
        return text.split(" ")[0].replace("/", "-")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text.split("T")[0]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(JAPAN).date().isoformat()


def parse(payload) -> Product | None:
    content = ((payload or {}).get("data") or {}).get("ppvContent") if isinstance(payload, dict) else None
    if not isinstance(content, dict) or not content.get("id"):
        return None
    maker = content.get("maker") if isinstance(content.get("maker"), dict) else {}
    seconds = content.get("duration")
    return Product(str(content["id"]).lower(), str(content.get("makerContentId") or "").strip(),
                   japan_date(content.get("deliveryStartDate") or ""), str(content.get("title") or "").strip(),
                   str(maker.get("name") or "").strip(),
                   round(seconds / 60) if isinstance(seconds, (int, float)) and seconds > 0 else 0)


def parse_digital(payload) -> str | None:
    summary = ((payload or {}).get("data") or {}).get("ppvContentSummaryByExternalId") if isinstance(payload, dict) else None
    return str(summary["id"]).lower() if isinstance(summary, dict) and summary.get("id") else None


def _ask(query: str, cid: str):
    """DMM's answer, or None when it can't be asked right now."""
    try:
        response = requests.post(
            URL,
            json={"query": query, "variables": {"id": cid}},
            headers={"Accept": "application/json", "User-Agent": f"clearjav-aio/{__version__}"},
            timeout=(10, 30),
        )
        if response.status_code != 200:
            log.info("DMM API for %s: HTTP %s", cid, response.status_code)
            return None
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        log.info("DMM API for %s: %s", cid, exc)
        return None


def lookup(cid: str) -> Product | None:
    """The product, or None when DMM doesn't know the id or can't be asked right now."""
    return parse(_ask(QUERY, cid))


def digital_id(cid: str) -> str | None:
    """DMM's digital listing for a content id (the same id when it is one already), or None."""
    return parse_digital(_ask(DIGITAL_QUERY, cid))
