"""Provenance-first corpus collection into portable Markdown records.

The library exposes an auditable record model and explicit source adapters. It does
not grant permission to collect content or bypass source access controls; operators
remain responsible for source terms, licenses, privacy, and applicable law.
"""

from .acquisition import CollectionResult, CollectionSpec, collect
from .assets import AssetStore, StoredAsset, merge_asset_manifest
from .base import BaseScraper, CorpusItem, WriteResult, write_corpus_item
from .browser import (
    BrowserPolicy,
    BrowserReceiptVerification,
    BrowserRenderFailure,
    PlaywrightBrowserDriver,
    RenderedPage,
    render_page,
    verify_browser_receipt,
    write_browser_receipt,
)
from .crawl import (
    CrawlPolicy,
    SiteCollectionResult,
    collect_site,
    verify_site_receipt,
    write_site_receipt,
)
from .evidence import build_evidence_index, query_evidence_index, verify_evidence_index
from .rights import AuthorizationDeclaration, RightsDeclaration, SourcePolicy
from .source_package import (
    SourcePackageManifest,
    discover_source_package,
    import_source_package,
    verify_import,
)

__version__ = "0.3.0.dev0"
__all__ = [
    "BaseScraper",
    "AssetStore",
    "AuthorizationDeclaration",
    "BrowserPolicy",
    "BrowserReceiptVerification",
    "BrowserRenderFailure",
    "CollectionResult",
    "CollectionSpec",
    "CorpusItem",
    "CrawlPolicy",
    "PlaywrightBrowserDriver",
    "RenderedPage",
    "RightsDeclaration",
    "SiteCollectionResult",
    "SourcePackageManifest",
    "SourcePolicy",
    "StoredAsset",
    "WriteResult",
    "build_evidence_index",
    "collect",
    "collect_site",
    "discover_source_package",
    "import_source_package",
    "merge_asset_manifest",
    "query_evidence_index",
    "render_page",
    "verify_evidence_index",
    "verify_browser_receipt",
    "verify_import",
    "verify_site_receipt",
    "write_site_receipt",
    "write_browser_receipt",
    "write_corpus_item",
    "__version__",
]
