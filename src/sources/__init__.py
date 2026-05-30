"""
src.sources — One module per job-board integration.

To add a new source: create src/sources/<name>.py exporting `fetch() -> list[Job]`,
register it in SOURCES dict in config.py, and import it in src/fetcher.py.
"""
