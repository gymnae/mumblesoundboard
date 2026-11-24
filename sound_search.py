"""Text and tag search over sound records (the dicts served by /meta).

Query syntax, shared with the search box on the main page:
    words      every word must appear in the file name, title, artist,
               album, genre, year or one of the tags
    #tag       the sound must carry exactly this tag
Both are case-insensitive and combine with AND.
"""
from collections import Counter

from sound_metadata import normalize_tags

SEARCH_FIELDS = ('name', 'title', 'artist', 'album', 'genre', 'year')


def parse_query(query, tags=()):
    """(words, required tags) from a query string plus extra ?tag= values."""
    words, required = [], list(tags)
    for token in (query or '').lower().split():
        if token.startswith('#') and len(token) > 1:
            required.append(token)
        else:
            words.append(token)
    return words, normalize_tags(required)


def haystack(info):
    """Lower-case text the words of a query are matched against."""
    parts = [str(info.get(field) or '') for field in SEARCH_FIELDS]
    parts += info.get('tags') or []
    return ' '.join(parts).lower()


def matches(info, words, required_tags):
    text = haystack(info)
    tags = set(info.get('tags') or [])
    return all(word in text for word in words) and all(tag in tags for tag in required_tags)


def search(infos, query='', tags=()):
    words, required = parse_query(query, tags)
    if not words and not required:
        return list(infos)
    return [info for info in infos if matches(info, words, required)]


def tag_counts(infos):
    """[{'tag', 'count'}], most used first, then alphabetical."""
    counts = Counter(tag for info in infos for tag in info.get('tags') or [])
    return [{'tag': tag, 'count': count} for tag, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
