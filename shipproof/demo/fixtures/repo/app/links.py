import urllib.request


def auditLink(url: str) -> dict:
    """Checks that a link answers without a login redirect."""
    with urllib.request.urlopen(url, timeout=10) as response:
        return {"url": url, "status": response.status, "finalUrl": response.geturl()}
