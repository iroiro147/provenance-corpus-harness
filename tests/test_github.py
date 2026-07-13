import base64

from harness.scrapers.github import GitHubScraper

META = {
    "full_name": "octocat/Hello-World",
    "html_url": "https://github.com/octocat/Hello-World",
    "description": "My first repo",
    "topics": ["demo", "example"],
    "owner": {"login": "octocat"},
    "pushed_at": "2026-06-01T00:00:00Z",
    "stargazers_count": 1500,
    "language": "Python",
}
README = {
    "encoding": "base64",
    "content": base64.b64encode(b"# Hello World\n\nThis is the README body.").decode(),
}
RELEASES = [{"name": "v1.0", "tag_name": "v1.0", "body": "First release notes."}]


def fake(url):
    if url.endswith("/readme"):
        return README
    if "/releases" in url:
        return RELEASES
    if url.endswith("/repos/octocat/Hello-World"):
        return META
    return {}


def test_github_repo_offline():
    gh = GitHubScraper(fetch_json=fake)
    items = list(gh.scrape("octocat/Hello-World", limit=10))

    assert len(items) == 1
    it = items[0]
    assert it.title == "octocat/Hello-World"
    assert it.author == "octocat"
    assert "My first repo" in it.body
    assert "Topics: demo, example" in it.body
    assert "This is the README body." in it.body  # base64 README decoded
    assert "First release notes." in it.body  # release notes included
    assert it.extra["stars"] == 1500 and it.extra["language"] == "Python"


def test_github_invalid_target_skipped():
    gh = GitHubScraper(fetch_json=lambda u: {})
    assert list(gh.scrape("not-a-repo")) == []  # no owner/repo slash → skipped
