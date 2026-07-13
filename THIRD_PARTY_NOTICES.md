# Third-party notices

The project directly depends on the following Python packages. The versions below are
the versions audited for the v0.1.0 release-preparation branch; allowed dependency
ranges are defined in `pyproject.toml`.

| Package | Audited version | License |
|---|---:|---|
| requests | 2.34.2 | Apache-2.0 |
| feedparser | 6.0.12 | BSD-2-Clause |
| trafilatura | 2.1.0 | Apache-2.0 |
| PyYAML | 6.0.3 | MIT |

These packages remain governed by their own licenses. This file is informational and
does not replace the license text or metadata distributed by each dependency.

The optional YouTube adapter invokes a separately installed `yt-dlp` executable. It is
not bundled with this package and is governed by its own license and source terms.
