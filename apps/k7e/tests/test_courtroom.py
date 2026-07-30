"""Courtroom test — progressive fact introduction + cross-examination.

Simulates real usage: facts introduced incrementally across "sessions",
then queried for specific recall accuracy. Named after the legal metaphor:
witnesses introduce testimony over time, then cross-examination tests
whether the system can recall specific details under questioning.
"""

import engine


# Facts introduced in waves (simulating sessions over days)
TESTIMONY = [
    # Wave 1: Infrastructure basics
    {"title": "Production server specs", "content": "Hetzner CX22: 4GB RAM, 2 vCPUs, 40GB SSD, Ubuntu 24.04.", "tags": ["infra"]},
    {"title": "Remote access via RustDesk", "content": "RustDesk ID 131350081. Uses dummy Xorg driver for headless XFCE.", "tags": ["infra"]},
    {"title": "Drive mount config", "content": "Google Drive at /mnt/gdrive via rclone. Flags: --vfs-cache-mode full --vfs-cache-max-size 10G.", "tags": ["infra", "storage"]},

    # Wave 2: Tool configurations
    {"title": "Chrome debugging for Playwright", "content": "Launch with --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-profile.", "tags": ["browser", "testing"]},
    {"title": "Port conflict resolution", "content": "Port 9222 conflicts with other debug tools. Use 9333+ for multiple instances.", "tags": ["browser", "troubleshooting"]},
    {"title": "FTS5 hyphen tokenization", "content": "SQLite FTS5 tokenizes on hyphens. Expand 'remote-debugging-port' to 'remote debugging port' for matching.", "tags": ["search", "sqlite"]},

    # Wave 3: Communication protocols
    {"title": "Message format for tell CLI", "content": "tell <recipient> <message> [FILE: path]. JSON envelope: id, date, to, content, files. ULIDs for ordering.", "tags": ["messaging"]},
    {"title": "SMS restriction", "content": "Never use email gateway for SMS. Phone communication via tell CLI only. Gateway is unreliable.", "tags": ["messaging", "safety"]},

    # Wave 4: Corrections and edge cases
    {"title": "Rclone mount timeout fix", "content": "If rclone fails with 'Daemon timed out', run without --daemon first. Ensure fuse3 installed.", "tags": ["infra", "troubleshooting"]},
    {"title": "Gemini headless requires yolo", "content": "Policy Engine does not apply in headless -p mode. --yolo required otherwise tool calls silently fail.", "tags": ["gemini", "troubleshooting"]},

    # Wave 5: Preferences and decisions
    {"title": "Brevity in communication", "content": "Keep responses brief and direct. No trailing summaries. No emojis unless asked.", "tags": ["style"]},
    {"title": "Dependency policy", "content": "Packages must be 8+ years old, actively maintained, widely used. Research before adopting.", "tags": ["policy"]},
    {"title": "Simplicity principle", "content": "20 lines of simple code over 200 lines of correct code. Three lines beat a premature abstraction.", "tags": ["policy"]},
]

# Cross-examination: queries that should find specific facts
CROSS_EXAMINATION = [
    # Exact technical recall
    ("what port for chrome remote debugging", "Chrome debugging for Playwright"),
    ("rclone cache size flag", "Drive mount config"),
    ("how to fix rclone timeout", "Rclone mount timeout fix"),
    ("gemini headless policy engine", "Gemini headless requires yolo"),
    ("FTS5 hyphens", "FTS5 hyphen tokenization"),

    # Conceptual recall
    ("how to keep code simple", "Simplicity principle"),
    ("what server specs", "Production server specs"),
    ("how to send messages", "Message format for tell CLI"),
    ("dependency requirements", "Dependency policy"),
    ("why not use email for texting", "SMS restriction"),

    # Disambiguation (multiple related entries exist)
    ("port conflict chrome", "Port conflict resolution"),
    ("communication style preferences", "Brevity in communication"),
    ("remote access to server", "Remote access via RustDesk"),
]


class TestCourtroomRecall:
    """Progressive fact introduction + cross-examination for recall accuracy."""

    def _introduce_testimony(self):
        ids = {}
        for item in TESTIMONY:
            nid = engine.store_entry(item["title"], item["content"], tags=item["tags"])
            ids[item["title"]] = nid
        return ids

    def test_all_testimony_stored(self, store):
        ids = self._introduce_testimony()
        assert len(ids) == len(TESTIMONY)
        nodes = engine.list_nodes()
        assert len(nodes) == len(TESTIMONY)

    def test_cross_examination_precision(self, store):
        """Each query should return the expected entry in top-3 results."""
        self._introduce_testimony()
        hits = 0
        misses = []
        for query, expected_title in CROSS_EXAMINATION:
            results = engine.search(query, limit=3)
            titles = [r["title"] for r in results]
            if expected_title in titles:
                hits += 1
            else:
                misses.append({"query": query, "expected": expected_title, "got": titles})

        precision = hits / len(CROSS_EXAMINATION)
        assert precision >= 0.6, (
            f"Precision {precision:.0%} ({hits}/{len(CROSS_EXAMINATION)}). "
            f"Misses: {misses}"
        )

    def test_zero_false_negatives_on_unique_terms(self, store):
        """Every entry should be findable by its title."""
        ids = self._introduce_testimony()
        misses = []
        for item in TESTIMONY:
            results = engine.search(item["title"], limit=5)
            found_ids = {r["id"] for r in results}
            if ids[item["title"]] not in found_ids:
                misses.append(item["title"])
        assert len(misses) == 0, f"False negatives (by title): {misses}"

    def test_no_false_positives_for_unrelated(self, store):
        """Queries about absent topics return empty or very low relevance."""
        self._introduce_testimony()
        unrelated = [
            "sourdough bread recipe fermentation",
            "FIFA world cup 2026 group stage",
            "nuclear fusion reactor containment",
        ]
        for q in unrelated:
            results = engine.search(q, limit=3)
            assert len(results) == 0, f"False positive for '{q}': {results}"

    def test_incremental_growth_stable(self, store):
        """Precision doesn't degrade as more entries are added."""
        # Store first 3
        for item in TESTIMONY[:3]:
            engine.store_entry(item["title"], item["content"], tags=item["tags"])
        r1 = engine.search("Hetzner server", limit=3)
        assert len(r1) >= 1

        # Store remaining
        for item in TESTIMONY[3:]:
            engine.store_entry(item["title"], item["content"], tags=item["tags"])
        r2 = engine.search("Hetzner server", limit=3)
        assert len(r2) >= 1
        # Original still in top results
        assert any("server" in r["title"].lower() or "Hetzner" in r["title"] for r in r2)

    def test_append_then_recall(self, store):
        """Appended information is findable by its unique terms."""
        nid = engine.store_entry("Base Entry", "Original content", tags=["test"])
        engine.append_entry(nid, "Edge Cases", "Unique xylophone correction discovered")
        results = engine.search("xylophone correction")
        assert len(results) >= 1
        assert results[0]["id"] == nid
