#!/usr/bin/env python3
"""
app.py — Flask web server wrapper for LEX_OIC_first_trial_window.py

This file does NOT duplicate any scraper logic.
It simply imports everything from your original file and serves a web UI on top.

Place this file in the SAME folder as LEX_OIC_first_trial_window.py
"""

import os
import sys
import json

# Add current directory to path so Python can find the original file
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import EVERYTHING from your original scraper file
from LEX_OIC_first_trial_window import (
    init_db,
    get_db_connection,
    get_recent_entries,
    save_entries,
    scrape_rbi_updates,
    export_to_json,
    DB_NAME,
    JSON_OUTPUT_FILE,
    logger
)

from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS

# Flask app setup
app = Flask(__name__, static_folder='.')
CORS(app)


def get_all_entries():
    """Get ALL entries from DB (not just recent 24h)."""
    import sqlite3
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, title, description, link, source, source_id, published_date, scraped_at
        FROM regulations
        ORDER BY published_date DESC, scraped_at DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_stats():
    """Get dashboard statistics."""
    import sqlite3
    from datetime import datetime, timedelta

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM regulations")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM regulations WHERE source = 'RSS'")
    rss = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM regulations WHERE source = 'PressRelease'")
    press = cursor.fetchone()[0]

    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    cursor.execute("SELECT COUNT(*) FROM regulations WHERE datetime(scraped_at) >= datetime(?)", (cutoff,))
    recent24h = cursor.fetchone()[0]

    conn.close()
    return {"total": total, "rss": rss, "press": press, "recent24h": recent24h}


# ==================== FLASK ROUTES ====================

@app.route('/')
def index():
    """Serve the main dashboard HTML."""
    return send_from_directory('.', 'index.html')


@app.route('/api/entries')
def api_entries():
    """Get all regulation entries with stats."""
    try:
        entries = get_all_entries()
        stats = get_stats()
        return jsonify({
            "success": True,
            "entries": entries,
            "stats": stats,
            "count": len(entries)
        })
    except Exception as e:
        logger.error(f"API error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/entries/recent')
def api_entries_recent():
    """Get entries from last N hours (default 24)."""
    try:
        hours = request.args.get('hours', 24, type=int)
        entries = get_recent_entries(hours)
        return jsonify({
            "success": True,
            "entries": entries,
            "count": len(entries)
        })
    except Exception as e:
        logger.error(f"API error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/scrape', methods=['POST'])
def api_scrape():
    """Trigger the scraper and return results."""
    try:
        logger.info("API: Triggering scraper...")
        all_entries = scrape_rbi_updates()
        saved_count = save_entries(all_entries)

        # Also export to JSON for backward compatibility
        recent = get_recent_entries(24)
        export_to_json(recent, JSON_OUTPUT_FILE)

        stats = get_stats()
        logger.info(f"Scrape complete. New entries: {saved_count}")

        return jsonify({
            "success": True,
            "new_entries": saved_count,
            "total_scraped": len(all_entries),
            "stats": stats,
            "message": f"Scraped {len(all_entries)} entries, saved {saved_count} new ones."
        })
    except Exception as e:
        logger.error(f"Scrape error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/stats')
def api_stats():
    """Get dashboard statistics only."""
    try:
        return jsonify({"success": True, "stats": get_stats()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/export/json')
def api_export_json():
    """Download the JSON export file."""
    try:
        if os.path.exists(JSON_OUTPUT_FILE):
            return send_from_directory('.', JSON_OUTPUT_FILE, as_attachment=True)
        else:
            return jsonify({"success": False, "error": "No export file found. Run scraper first."}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== MAIN ====================
if __name__ == '__main__':
    init_db()

    stats = get_stats()
    if stats["total"] == 0:
        logger.info("Database is empty. Click 'Run Scraper' in the dashboard to populate data.")

    logger.info("=" * 60)
    logger.info("RBI Regulations Dashboard Server")
    logger.info("=" * 60)
    logger.info("Open http://127.0.0.1:5000 in your browser")
    logger.info("API endpoints:")
    logger.info("  GET  /api/entries        - All entries")
    logger.info("  GET  /api/entries/recent - Last 24h entries")
    logger.info("  POST /api/scrape         - Run scraper")
    logger.info("  GET  /api/stats          - Dashboard stats")
    logger.info("  GET  /api/export/json    - Download JSON")
    logger.info("=" * 60)

    app.run(host='0.0.0.0', port=5000, debug=True)

