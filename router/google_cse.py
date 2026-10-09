"""Google Custom Search JSON API adapter; transport and filtering belong to the router."""

import urllib.parse
import urllib.request


SEARCH_URL = "https://customsearch.googleapis.com/customsearch/v1"


def search(query, engine, domains, language, api_key, cx, request_json):
    """Return normalized links, or raise ValueError for an invalid API response.

    request_json must enforce the caller's HTTP deadline. It receives one Request;
    this adapter neither retries requests nor manages credential budgets.
    """
    if domains:
        query = "(%s) (%s)" % (query, " OR ".join("site:" + domain for domain in domains))
    params = {"key": api_key, "cx": cx, "q": query, "num": 10}
    if language:
        params["hl"] = language
    request = urllib.request.Request(
        SEARCH_URL + "?" + urllib.parse.urlencode(params),
        headers={"Accept": "application/json"},
    )
    data = request_json(request)
    if not isinstance(data, dict) or "error" in data:
        raise ValueError("Google CSE returned an invalid response or API error")
    if data.get("kind") != "customsearch#search":
        raise ValueError("Google CSE returned an unexpected response kind")
    items = data.get("items", [])
    if not isinstance(items, list):
        raise ValueError("Google CSE returned invalid items")
    results = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("link"), str):
            raise ValueError("Google CSE returned an invalid result")
        title, snippet = item.get("title", ""), item.get("snippet", "")
        if not isinstance(title, str) or not isinstance(snippet, str):
            raise ValueError("Google CSE returned invalid result text")
        results.append({"url": item["link"], "title": title,
                        "content": snippet, "engine": engine})
    return {"results": results}
