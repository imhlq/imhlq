#!/usr/bin/env python3
"""Generate three SVG GitHub star badges from public owned and contributed repos.

Uses stdlib only. Search includes commits attributed to the GitHub login and
merged PRs authored by the login. It deliberately rejects capped searches.
"""
import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
USER = os.environ.get('GH_USERNAME', 'imhlq').strip()
TOKEN = os.environ.get('GH_TOKEN', '')
API = 'https://api.github.com'
HEADERS = {
    'Accept': 'application/vnd.github+json',
    'User-Agent': 'public-star-badge-generator',
    'X-GitHub-Api-Version': '2022-11-28',
}
if TOKEN:
    HEADERS['Authorization'] = f'Bearer {TOKEN}'


def api(path, params=None):
    url = API + path + ('?' + urlencode(params) if params else '')
    request = Request(url, headers=HEADERS)
    for attempt in range(4):
        try:
            with urlopen(request, timeout=35) as response:
                return json.load(response)
        except HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')[:500]
            if exc.code in (403, 429) and attempt < 3:
                reset = int(exc.headers.get('X-RateLimit-Reset', '0'))
                wait = min(65, max(3, reset - int(time.time()) + 2))
                print(f'Rate limited; retrying in {wait}s', file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(f'GitHub API HTTP {exc.code} for {path}: {body}') from exc
        except URLError as exc:
            raise RuntimeError(f'Network error for {path}: {exc}') from exc
    raise RuntimeError(f'Retries exhausted: {path}')


def paged(path, params=None):
    """Return all pages of a normal (non-search) REST collection."""
    result = []
    for page in range(1, 1001):
        chunk = api(path, {**(params or {}), 'per_page': 100, 'page': page})
        if not isinstance(chunk, list):
            raise RuntimeError(f'Unexpected API response from {path}')
        result.extend(chunk)
        if len(chunk) < 100:
            return result
    raise RuntimeError(f'Pagination safety limit exceeded: {path}')


def search_repositories(query, kind):
    """Search with strict 1,000-result guard; prevents partial totals."""
    result = []
    for page in range(1, 11):
        response = api(f'/search/{kind}', {'q': query, 'per_page': 100, 'page': page})
        total = response['total_count']
        if response.get('incomplete_results') or total > 1000:
            raise RuntimeError(
                f'GitHub search is incomplete/capped for {query!r} ({total} results). '
                'Split the search by date before trusting the badge.')
        items = response['items']
        result.extend(items)
        if len(result) >= total or len(items) < 100:
            break
    if len(result) != total:
        raise RuntimeError(f'Search returned {len(result)} of {total} results for {query!r}')
    names = set()
    for item in result:
        repo = item.get('repository') if kind == 'commits' else item.get('repository_url')
        if kind == 'commits' and isinstance(repo, dict):
            names.add(repo['full_name'])
        elif kind == 'issues' and isinstance(repo, str):
            names.add(repo.rsplit('/repos/', 1)[-1])
    return names


def badge(label, count, color):
    number = f'{count:,}'
    # Fixed-character estimates leave sufficient breathing room for each label.
    left = max(100, len(label) * 7 + 26)
    right = max(56, len(number) * 9 + 24)
    width = left + right
    label_xml, number_xml = escape(label), escape(number)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="28" role="img" aria-label="{label_xml}: {number_xml}">
  <title>{label_xml}: {number_xml}</title>
  <linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#fff" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>
  <clipPath id="r"><rect width="{width}" height="28" rx="5"/></clipPath>
  <g clip-path="url(#r)"><rect width="{left}" height="28" fill="#24292f"/><rect x="{left}" width="{right}" height="28" fill="{color}"/><rect width="{width}" height="28" fill="url(#s)"/></g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,DejaVu Sans,sans-serif" font-size="12" font-weight="600">
    <text x="{left / 2}" y="18">{label_xml}</text><text x="{left + right / 2}" y="18">{number_xml}</text>
  </g>
</svg>
'''


def main():
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', USER):
        raise ValueError('Invalid GitHub username')
    owned = {repo['full_name'] for repo in paged(f'/users/{USER}/repos', {'type': 'owner'})
             if not repo['private']}
    committed = search_repositories(f'author:{USER}', 'commits')
    merged_prs = search_repositories(f'is:pr is:merged author:{USER}', 'issues')
    discovered = committed | merged_prs
    contributed = discovered - owned

    # Resolve each name anew: current owner, visibility and star count are authoritative.
    # An inaccessible/deleted repository is skipped with an audit note.
    details = {}
    unavailable = []
    for name in sorted(owned | contributed, key=str.casefold):
        try:
            repo = api('/repos/' + name)
        except RuntimeError as exc:
            if 'HTTP 404' in str(exc):
                unavailable.append(name)
                continue
            raise
        if repo.get('private'):
            unavailable.append(name)
            continue
        details[repo['id']] = {
            'name': repo['full_name'],
            'stars': repo['stargazers_count'],
            'owned': repo['owner']['login'].casefold() == USER.casefold(),
            'contributed': name in contributed,
        }

    # Classify by current ownership; identical repos cannot be counted twice.
    own_sum = sum(r['stars'] for r in details.values() if r['owned'])
    other_sum = sum(r['stars'] for r in details.values() if not r['owned'])
    total = own_sum + other_sum
    badges = ROOT / 'badges'
    data = ROOT / 'data'
    badges.mkdir(exist_ok=True)
    data.mkdir(exist_ok=True)
    for filename, label, count, color in [
        ('owned.svg', 'Owned stars', own_sum, '#238636'),
        ('contributed.svg', 'Contributed stars', other_sum, '#0969da'),
        ('combined.svg', 'Combined stars', total, '#bf8700'),
    ]:
        (badges / filename).write_text(badge(label, count, color), encoding='utf-8')
    output = {
        'username': USER,
        'updated_at_utc': datetime.now(timezone.utc).isoformat(),
        'definition': 'Current stars on public owned repos and distinct public repos with authored commits or merged PRs',
        'owned_stars': own_sum,
        'contributed_stars': other_sum,
        'combined_stars': total,
        'unavailable_repositories': unavailable,
        'repositories': sorted(details.values(), key=lambda r: r['name'].casefold()),
    }
    (data / 'stars.json').write_text(json.dumps(output, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'Owned: {own_sum} | Contributed: {other_sum} | Combined: {total}; repos: {len(details)}')
    if unavailable:
        print('Skipped inaccessible repos:', ', '.join(unavailable), file=sys.stderr)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, KeyError) as error:
        sys.exit(f'ERROR: {error}')
