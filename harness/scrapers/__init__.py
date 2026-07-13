"""Explicit source adapters with offline-testable collection contracts."""

from .blog import BlogScraper
from .github import GitHubScraper
from .hackernews import HackerNewsScraper
from .producthunt import ProductHuntScraper
from .reddit import RedditScraper
from .rss import RSSScraper
from .youtube import YouTubeScraper

# Registry the CLI dispatches on.
SCRAPERS = {
    "hackernews": HackerNewsScraper,
    "rss": RSSScraper,
    "blog": BlogScraper,
    "youtube": YouTubeScraper,
    "reddit": RedditScraper,
    "producthunt": ProductHuntScraper,
    "github": GitHubScraper,
}

__all__ = [
    "HackerNewsScraper",
    "RSSScraper",
    "BlogScraper",
    "YouTubeScraper",
    "RedditScraper",
    "ProductHuntScraper",
    "GitHubScraper",
    "SCRAPERS",
]
