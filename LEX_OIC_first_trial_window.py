#!/usr/bin/env python3
"""
LEX_OIC_first_trial_window.py

Scrapes RBI (Reserve Bank of India) regulations, notifications, and press releases
from the past 24 hours. Outputs data to a JSON file and stores it in a SQLite database
with deduplication - running the script multiple times within 24 hours will not
duplicate entries.

Data sources:
- RBI Notifications RSS: https://www.rbi.org.in/notifications_rss.xml
- RBI Press Releases page: https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx
"""

import feedparser
import json
import sqlite3
import hashlib
import re
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from typing import List, Dict, Optional, Any
import logging
import sys

# ==================== CONFIGURATION ====================
DB_NAME = "rbi_regulations.db"
JSON_OUTPUT_FILE = "rbi_regulations_24h.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ==================== LOGGING SETUP ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


# ==================== DATABASE LAYER ====================
def get_db_connection() -> sqlite3.Connection:
    """Create and return a connection to the SQLite database."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Initialize the database with the regulations table if it doesn't exist.
    Uses a composite unique constraint on (source, source_id) to prevent duplicates.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS regulations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            link TEXT,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            published_date TEXT,
            scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
            raw_data TEXT,
            UNIQUE(source, source_id)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_published_date 
        ON regulations(published_date)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_scraped_at 
        ON regulations(scraped_at)
    """)
    conn.commit()
    conn.close()
    logger.info(f"Database '{DB_NAME}' initialized successfully.")


def entry_exists(conn: sqlite3.Connection, source: str, source_id: str) -> bool:
    """Check if an entry with the given source and source_id already exists."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM regulations WHERE source = ? AND source_id = ?",
        (source, source_id)
    )
    return cursor.fetchone() is not None


def save_entries(entries: List[Dict[str, Any]]) -> int:
    """
    Save a list of regulation entries to the database, skipping duplicates.
    Returns the number of new entries saved.
    """
    if not entries:
        return 0

    conn = get_db_connection()
    cursor = conn.cursor()
    saved_count = 0

    for entry in entries:
        source = entry.get("source", "unknown")
        source_id = entry.get("source_id", "")
        if not source_id:
            # Generate a hash-based ID if none provided
            hash_input = f"{source}|{entry.get('title', '')}|{entry.get('link', '')}"
            source_id = hashlib.sha256(hash_input.encode()).hexdigest()[:16]

        if entry_exists(conn, source, source_id):
            logger.debug(f"Skipping duplicate: {source} - {source_id}")
            continue

        try:
            cursor.execute("""
                INSERT INTO regulations (
                    title, description, link, source, source_id,
                    published_date, raw_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.get("title", ""),
                entry.get("description", ""),
                entry.get("link", ""),
                source,
                source_id,
                entry.get("published_date"),
                json.dumps(entry, default=str)
            ))
            saved_count += 1
        except sqlite3.IntegrityError:
            logger.debug(f"Duplicate entry (race condition): {source} - {source_id}")
            continue

    conn.commit()
    conn.close()
    return saved_count


def get_recent_entries(hours: int = 24) -> List[Dict[str, Any]]:
    """
    Retrieve entries from the last N hours from the database.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cutoff = datetime.now() - timedelta(hours=hours)
    cursor.execute("""
        SELECT title, description, link, source, source_id, published_date, scraped_at
        FROM regulations
        WHERE datetime(scraped_at) >= datetime(?)
        ORDER BY published_date DESC, scraped_at DESC
    """, (cutoff.isoformat(),))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ==================== SCRAPING FUNCTIONS ====================
def fetch_url_content(url: str) -> Optional[str]:
    """Fetch content from a URL with a proper User-Agent header."""
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=30) as response:
            content = response.read().decode("utf-8", errors="ignore")
            return content
    except HTTPError as e:
        logger.error(f"HTTP error fetching {url}: {e.code} - {e.reason}")
    except URLError as e:
        logger.error(f"URL error fetching {url}: {e.reason}")
    except Exception as e:
        logger.error(f"Unexpected error fetching {url}: {e}")
    return None


def parse_rss_feed(url: str) -> List[Dict[str, Any]]:
    """
    Parse an RSS feed and extract entries with published dates.
    Returns a list of entry dictionaries.
    """
    entries = []
    content = fetch_url_content(url)
    if not content:
        return entries

    feed = feedparser.parse(content)
    if feed.bozo:
        logger.warning(f"Feed parsing warning: {feed.bozo_exception}")

    cutoff = datetime.now() - timedelta(hours=24)

    for item in feed.entries:
        try:
            # Parse published date
            pub_date = None
            if hasattr(item, "published_parsed") and item.published_parsed:
                pub_date = datetime(*item.published_parsed[:6])
            elif hasattr(item, "updated_parsed") and item.updated_parsed:
                pub_date = datetime(*item.updated_parsed[:6])

            if pub_date and pub_date < cutoff:
                continue  # Skip entries older than 24 hours

            # Extract GUID or generate a unique ID
            source_id = getattr(item, "id", "") or getattr(item, "guid", "") or ""
            if not source_id and hasattr(item, "link"):
                source_id = hashlib.sha256(item.link.encode()).hexdigest()[:16]

            entry = {
                "title": getattr(item, "title", ""),
                "description": getattr(item, "description", "") or getattr(item, "summary", ""),
                "link": getattr(item, "link", ""),
                "source": "RSS",
                "source_id": source_id,
                "published_date": pub_date.isoformat() if pub_date else None,
                "raw_feed_item": {
                    k: v for k, v in item.items()
                    if k in ["title", "description", "summary", "link", "id", "guid"]
                }
            }
            entries.append(entry)
        except Exception as e:
            logger.warning(f"Error parsing RSS item: {e}")

    logger.info(f"Found {len(entries)} entries from RSS feed (last 24h)")
    return entries


def scrape_press_releases() -> List[Dict[str, Any]]:
    """
    Scrape press releases from the RBI website.
    Since the press release page shows a list with dates, we fetch and parse it.
    """
    entries = []
    base_url = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx"
    content = fetch_url_content(base_url)
    if not content:
        return entries

    cutoff = datetime.now() - timedelta(hours=24)
    # Look for date patterns like "Date : Jul 17, 2026"
    date_pattern = re.compile(r'Date\s*:\s*([A-Za-z]{3}\s+\d{1,2},\s*\d{4})', re.IGNORECASE)
    # Look for press release titles/links
    link_pattern = re.compile(r'<a\s+[^>]*href="([^"]*)"[^>]*>([^<]*)</a>', re.IGNORECASE)

    lines = content.split('\n')
    current_date = None
    potential_entries = []

    for i, line in enumerate(lines):
        # Check for a date
        date_match = date_pattern.search(line)
        if date_match:
            try:
                date_str = date_match.group(1)
                current_date = datetime.strptime(date_str, "%b %d, %Y")
            except ValueError:
                current_date = None

        # Check for links that might be press releases
        link_matches = link_pattern.findall(line)
        for href, link_text in link_matches:
            link_text = re.sub(r'<[^>]+>', '', link_text).strip()
            if not link_text or len(link_text) < 10:
                continue
            # Skip navigation/utility links
            if any(skip in href.lower() for skip in ['javascript:', '#', '?mode']):
                continue

            pub_date = current_date
            if pub_date and pub_date < cutoff:
                continue

            # Build full URL
            if href.startswith('http'):
                full_url = href
            elif href.startswith('/'):
                full_url = f"https://www.rbi.org.in{href}"
            else:
                full_url = f"https://www.rbi.org.in/Scripts/{href}"

            # Generate a unique ID
            source_id = hashlib.sha256(f"{full_url}|{link_text}".encode()).hexdigest()[:16]

            entry = {
                "title": link_text,
                "description": f"Press release: {link_text}",
                "link": full_url,
                "source": "PressRelease",
                "source_id": source_id,
                "published_date": pub_date.isoformat() if pub_date else None,
            }
            potential_entries.append(entry)

    # Filter out entries older than 24 hours
    for entry in potential_entries:
        if entry.get("published_date"):
            try:
                pub_dt = datetime.fromisoformat(entry["published_date"])
                if pub_dt >= cutoff:
                    entries.append(entry)
            except (ValueError, TypeError):
                entries.append(entry)
        else:
            entries.append(entry)

    logger.info(f"Found {len(entries)} entries from Press Releases (last 24h)")
    return entries


def scrape_rbi_updates() -> List[Dict[str, Any]]:
    """
    Master function to scrape all RBI regulation updates from various sources.
    """
    all_entries = []

    # 1. RSS Feed - Notifications
    rss_url = "https://www.rbi.org.in/notifications_rss.xml"
    logger.info(f"Fetching RSS feed: {rss_url}")
    rss_entries = parse_rss_feed(rss_url)
    all_entries.extend(rss_entries)

    # 2. Press Releases
    logger.info("Scraping Press Releases page...")
    pr_entries = scrape_press_releases()
    all_entries.extend(pr_entries)

    # 3. Additional RSS feed for general news (if available)
    # Some RBI feeds: https://www.rbi.org.in/rss/notifications.xml
    # Try alternative RSS endpoints
    alt_rss_urls = [
        "https://www.rbi.org.in/rss/notifications.xml",
        "https://www.rbi.org.in/rss/pressreleases.xml",
    ]
    for alt_url in alt_rss_urls:
        try:
            logger.info(f"Trying alternative RSS: {alt_url}")
            alt_entries = parse_rss_feed(alt_url)
            all_entries.extend(alt_entries)
        except Exception as e:
            logger.warning(f"Alternative RSS failed: {e}")

    return all_entries


# ==================== JSON OUTPUT ====================
def export_to_json(entries: List[Dict[str, Any]], filename: str) -> None:
    """Export entries to a JSON file."""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"Exported {len(entries)} entries to '{filename}'")


# ==================== MAIN ====================
def main() -> None:
    """Main entry point for the script."""
    logger.info("=" * 60)
    logger.info("LEX_OIC_first_trial_window - RBI Regulations Scraper")
    logger.info("=" * 60)

    # Initialize database
    init_db()

    # Scrape data from all sources
    logger.info("Starting data collection from RBI sources...")
    all_entries = scrape_rbi_updates()

    if not all_entries:
        logger.warning("No entries found in the last 24 hours.")
        # Still export empty JSON and show recent entries from DB
        recent = get_recent_entries(24)
        if recent:
            logger.info(f"Found {len(recent)} entries in database from last 24 hours.")
            export_to_json(recent, JSON_OUTPUT_FILE)
        else:
            export_to_json([], JSON_OUTPUT_FILE)
        return

    # Save to database (deduplication happens here)
    saved_count = save_entries(all_entries)
    logger.info(f"Saved {saved_count} new entries to database.")

    # Get all entries from the last 24 hours (including previously saved ones)
    recent_entries = get_recent_entries(24)
    logger.info(f"Total entries in last 24 hours: {len(recent_entries)}")

    # Export to JSON
    export_to_json(recent_entries, JSON_OUTPUT_FILE)

    # Summary
    logger.info("=" * 60)
    logger.info(f"SUMMARY:")
    logger.info(f"  - New entries scraped: {len(all_entries)}")
    logger.info(f"  - New entries saved to DB: {saved_count}")
    logger.info(f"  - Total entries in last 24h: {len(recent_entries)}")
    logger.info(f"  - Database: {DB_NAME}")
    logger.info(f"  - JSON output: {JSON_OUTPUT_FILE}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

