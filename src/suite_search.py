#!/usr/bin/env python3
"""Standalone online literature search GUI. Python 3.10+, tkinter, requests.

Only uses online result records in memory. Does not import the PDF library GUI,
open its database, or scan local PDFs. Metadata resolver is embedded from the
existing downloader core; the UI independently follows its native Tk layout.
External config lives beside this script or beside the PyInstaller executable.
"""
from __future__ import annotations

import copy
import html
import json
from pathlib import Path
import re
import sys
import threading
import time
import urllib.parse

import requests

FIELDS = ('title', 'authors', 'year', 'journal', 'volume', 'issue', 'pages',
          'article_number', 'date', 'journal_abbreviation', 'issn', 'language', 'abstract', 'publisher')

REQUIRED = tuple(field for field in FIELDS if field not in ('pages', 'article_number'))

ORDER = ('crossref', 'openalex', 'elsevier', 'wos')

PROVIDER_LABELS = {'crossref': 'Crossref', 'openalex': 'OpenAlex', 'elsevier': 'Elsevier', 'wos': 'WOS'}

OTHER_PUBLISHERS = ('wiley', 'springer', 'nature', 'mdpi', 'public library of science',
                    'plos', 'ieee', 'american chemical society', 'royal society of chemistry',
                    'taylor & francis', 'taylor and francis', 'sage publications',
                    'oxford university press', 'cambridge university press', 'iop publishing',
                    'american society of civil engineers', 'frontiers media')

LABELS = {'doi': 'DOI', 'title': '标题', 'authors': '作者（每行一人，姓, 名）',
          'year': '年份', 'journal': '期刊', 'volume': '卷', 'issue': '期',
          'pages': '页码', 'article_number': '文章号', 'issn': 'ISSN',
          'abstract': '摘要', 'publisher': '出版社', 'date': '出版日期',
          'journal_abbreviation': '期刊缩写', 'language': '语言'}

def clean(value):
    if value is None:
        return ''
    if isinstance(value, dict):
        value = value.get('$', '')
    return ' '.join(html.unescape(re.sub(r'<[^>]*>', '', str(value))).split())

def doi_normalize(value):
    value = urllib.parse.unquote(str(value or '').strip())
    value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)', '', value, flags=re.I)
    return value.lower() if re.fullmatch(r'10\.\d{4,9}/[^\s"\x00-\x1f]+', value, re.I) else ''

def provider_available(config, provider):
    return provider == 'crossref' or bool(str((config.get('api_keys') or {}).get(provider) or '').strip())

def enabled_providers(config):
    enabled = config.get('enabled_apis', ORDER)
    return [p for p in ORDER if p in enabled and provider_available(config, p)]


def sciencedirect_url(value):
    try:
        host = urllib.parse.urlsplit(str(value or '')).hostname or ''
        return host.lower() in ('sciencedirect.com', 'www.sciencedirect.com',
                                'api.elsevier.com', 'linkinghub.elsevier.com')
    except ValueError:
        return False


def publisher_affinity(doi, record=None):
    """Routing evidence, not a universal assertion of DOI-prefix ownership."""
    record = record or {}
    publisher = clean(record.get('publisher')).casefold()
    if (record.get('publisher_platform') == 'sciencedirect' or
            record.get('_platform') == 'sciencedirect' or
            doi_normalize(doi).startswith(('10.1016/', '10.1006/')) or
            'elsevier' in publisher or 'pergamon' in publisher or 'academic press' in publisher):
        return 'elsevier'
    if any(name in publisher for name in OTHER_PUBLISHERS):
        return 'other'
    return 'unknown'


def official_record_ready(record):
    # Issue, abbreviation and language are not guaranteed by Article API.
    # Do not query several other services just for those after official success.
    return (all(record.get(field) for field in ('title', 'authors', 'year', 'journal', 'volume', 'abstract'))
            and bool(record.get('pages') or record.get('article_number')))

def authors_from(value):
    if isinstance(value, str):
        return [clean(v) for v in value.split('|') if clean(v)]
    if isinstance(value, dict):
        value = [value]
    output = []
    for author in value or []:
        if isinstance(author, str):
            name = clean(author)
        else:
            name = ', '.join(clean(author.get(k)) for k in ('family', 'given') if clean(author.get(k)))
            name = name or clean(author.get('name') or author.get('displayName') or
                                 author.get('wosStandard') or author.get('$'))
        if name:
            output.append(name)
    return output

def crossref_record(data):
    m = data.get('message', {})
    year = ''
    date = ''
    for key in ('published-print', 'published', 'published-online', 'issued'):
        parts = (m.get(key) or {}).get('date-parts') or []
        if parts and parts[0]:
            year = str(parts[0][0])
            date = '-'.join(str(value) if i == 0 else f'{value:02d}' for i, value in enumerate(parts[0][:3]))
            break
    urls = [m.get('URL'), ((m.get('resource') or {}).get('primary') or {}).get('URL')]
    urls.extend(link.get('URL') for link in (m.get('link') or []) if isinstance(link, dict))
    return {'doi': m.get('DOI'), 'title': (m.get('title') or [''])[0],
            'authors': authors_from(m.get('author')), 'year': year,
            'journal': (m.get('container-title') or [''])[0], 'volume': m.get('volume'),
            'issue': m.get('issue'), 'pages': m.get('page'), 'article_number': m.get('article-number'),
            'issn': (m.get('ISSN') or [''])[0], 'abstract': m.get('abstract'),
            'publisher': m.get('publisher'), 'date': date, 'language': m.get('language'),
            'journal_abbreviation': (m.get('short-container-title') or [''])[0],
            '_platform': 'sciencedirect' if any(sciencedirect_url(url) for url in urls) else ''}

def wos_record(data, doi):
    for hit in data.get('hits', []):
        ids = hit.get('identifiers') or {}
        if doi_normalize(ids.get('doi')) != doi:
            continue
        source = hit.get('source') or {}
        pages = source.get('pages') or {}
        return {'doi': ids.get('doi'), 'title': hit.get('title'),
                'authors': authors_from((hit.get('names') or {}).get('authors')),
                'year': source.get('publishYear'), 'date': source.get('publishYear'), 'journal': source.get('sourceTitle'),
                'volume': source.get('volume'), 'issue': source.get('issue'),
                'pages': pages.get('range') or '-'.join(str(pages[k]) for k in ('begin', 'end') if pages.get(k)),
                'article_number': source.get('articleNumber'), 'issn': ids.get('issn') or ids.get('eissn')}
    return {}

def openalex_record(data):
    source = (data.get('primary_location') or {}).get('source') or {}
    biblio = data.get('biblio') or {}
    authorships = data.get('authorships') or []
    authors = [clean((a.get('author') or {}).get('display_name') or a.get('raw_author_name'))
               for a in authorships]
    # Do not advertise a truncated authorship list as a complete citation.
    if data.get('authors_count', len(authors)) > len(authors):
        authors = []
    abstract = data.get('abstract_inverted_index') or {}
    words = sorted((i, word) for word, indices in abstract.items() for i in indices)
    first, last = clean(biblio.get('first_page')), clean(biblio.get('last_page'))
    return {'doi': data.get('doi'), 'title': data.get('title') or data.get('display_name'),
            'authors': [a for a in authors if a], 'year': data.get('publication_year'),
            'journal': source.get('display_name'), 'volume': biblio.get('volume'),
            'issue': biblio.get('issue'), 'pages': first + ('-' + last if last and last != first and first else ''),
            'issn': source.get('issn_l') or next(iter(source.get('issn') or []), ''),
            'abstract': ' '.join(word for _, word in words),
            'publisher': source.get('host_organization_name'), 'date': data.get('publication_date'),
            'language': data.get('language'),
            '_platform': 'sciencedirect' if sciencedirect_url((data.get('primary_location') or {}).get('landing_page_url')) else ''}

def elsevier_record(data):
    core = (data.get('full-text-retrieval-response') or {}).get('coredata') or {}
    author_list = core.get('authors')
    if not author_list:
        creators = core.get('dc:creator')
        # The API documents dc:creator as first-author only. A singleton
        # must not prevent WOS from supplying the complete author list.
        author_list = creators if isinstance(creators, list) and len(creators) > 1 else []
    if isinstance(author_list, dict) and 'author' in author_list:
        author_list = author_list['author']
    return {'doi': core.get('prism:doi'), 'title': core.get('dc:title'),
            'authors': authors_from(author_list), 'year': str(core.get('prism:coverDate') or '')[:4],
            'journal': core.get('prism:publicationName'), 'volume': core.get('prism:volume'),
            'issue': core.get('prism:issueIdentifier'),
            'pages': core.get('prism:pageRange') or '-'.join(str(core[k]) for k in ('prism:startingPage', 'prism:endingPage') if core.get(k)),
            'article_number': core.get('articleNumber') or core.get('prism:articleNumber'), 'issn': core.get('prism:issn'),
            'abstract': core.get('dc:description'), 'publisher': core.get('prism:publisher') or core.get('dc:publisher'),
            'date': core.get('prism:coverDate'), 'language': core.get('dc:language'),
            '_platform': 'sciencedirect' if core.get('prism:doi') and core.get('dc:title') else ''}

def missing(record):
    fields = [f for f in REQUIRED if not record.get(f)]
    if not record.get('pages') and not record.get('article_number'):
        fields.append('pages')
    return fields

def status_of(record):
    record = trusted_record(record)
    if not record.get('doi'):
        return '需填写 DOI'
    if not record.get('title') and not record.get('attempts') and not record.get('updated_at'):
        return '待补全'
    return '缺失部分' if missing(record) else '已补全'

def missing_text(record):
    return '、'.join('页码或文章号' if field == 'pages' else LABELS[field].split('（')[0]
                    for field in missing(record)) or '无'

def trusted_record(record):
    """Retain old generated values in an archive, never silently certify them."""
    record = copy.deepcopy(record)
    sources = record.setdefault('field_sources', {})
    for field in list(sources):
        if sources[field] == 'openrouter':
            record.setdefault('legacy_generated_fields', {})[field] = record.pop(field, None)
            sources.pop(field)
    return record

class MetadataHTTPError(RuntimeError):
    def __init__(self, status, paused=False):
        self.status = status
        super().__init__(f'HTTP {status}' + ('；本批次暂停此渠道' if paused else ''))

class Resolver:
    def __init__(self, config, stop=None):
        self.config = config
        self.stop = stop or threading.Event()
        self.http = requests.Session()
        self.direct = requests.Session()
        self.direct.trust_env = False  # Preserve downloader's Elsevier institutional IP behavior.
        self.disabled = set()
        self.request_timings = []
        self.fields_needed = None
        self.publisher_hint = {}

    def close(self):
        self.http.close()
        self.direct.close()

    def request(self, provider, method, url, **kwargs):
        session = self.direct if provider == 'elsevier' else self.http
        for attempt in range(2):
            if self.stop.is_set():
                raise RuntimeError('已停止')
            started = time.perf_counter()
            response = None
            timing = {'provider': provider, 'method': method, 'attempt': attempt + 1,
                      'view': str((kwargs.get('params') or {}).get('view') or '')}
            try:
                response = session.request(method, url, timeout=(8, int(self.config.get('timeout', 25))), **kwargs)
                payload = response.json() if response.ok else None
                timing['outcome'] = 'ok' if response.ok else f'HTTP {response.status_code}'
            except Exception as error:
                timing['outcome'] = type(error).__name__
                raise
            finally:
                timing['http_status'] = response.status_code if response is not None else None
                timing['elapsed_seconds'] = round(time.perf_counter() - started, 3)
                self.request_timings.append(timing)
            if response.status_code in (401, 403, 429):
                # Avoid exhausting quotas / hammering a bad key across thousands of PDFs.
                self.disabled.add(provider)
            if response.status_code >= 500 and attempt == 0 and method == 'GET':
                if self.stop.wait(1):
                    raise RuntimeError('已停止')
                continue
            if not response.ok:
                raise MetadataHTTPError(response.status_code, provider in self.disabled)
            return payload
        return {}

    def fetch(self, provider, doi):
        keys = self.config.get('api_keys') or {}
        headers = {'Accept': 'application/json', 'User-Agent': 'PaperMetadataLibrary/1.0'}
        if provider in self.disabled:
            raise RuntimeError('本批次已暂停（鉴权失败或限流）')
        if provider == 'crossref':
            if keys.get('crossref'):
                headers['Crossref-Plus-API-Token'] = 'Bearer ' + keys['crossref']
            return crossref_record(self.request(provider, 'GET',
                'https://api.crossref.org/works/' + urllib.parse.quote(doi, safe=''), headers=headers,
                params={'mailto': self.config['contact_email']} if self.config.get('contact_email') else {}))
        if not keys.get(provider):
            raise RuntimeError('未配置 Key，跳过')
        if provider == 'openalex':
            return openalex_record(self.request(provider, 'GET',
                'https://api.openalex.org/works/https://doi.org/' + urllib.parse.quote(doi, safe=''),
                headers=headers, params={'api_key': keys['openalex']}))
        if provider == 'wos':
            headers['X-ApiKey'] = keys['wos']
            return wos_record(self.request(provider, 'GET',
                'https://api.clarivate.com/apis/wos-starter/v1/documents', headers=headers,
                params={'q': f'DO=("{doi}")', 'limit': 5, 'db': 'WOS'}), doi)
        if provider == 'elsevier':
            affinity = publisher_affinity(doi, self.publisher_hint)
            if affinity == 'other':
                raise RuntimeError('已确认为其他出版社，跳过 ScienceDirect Article API')
            headers['X-ELS-APIKey'] = keys['elsevier']
            if keys.get('elsevier_inst_token'):
                headers['X-ELS-Insttoken'] = keys['elsevier_inst_token']
            url = 'https://api.elsevier.com/content/article/doi/' + urllib.parse.quote(doi, safe='')
            needs_abstract = self.fields_needed is None or 'abstract' in self.fields_needed
            view = 'META_ABS' if needs_abstract and affinity == 'elsevier' and 'elsevier_abstract' not in self.disabled else 'META'
            try:
                data = self.request(provider, 'GET', url, headers=headers,
                                    params={'view': view, 'httpAccept': 'application/json'})
            except MetadataHTTPError as error:
                if error.status != 403 or view != 'META_ABS':
                    raise
                self.disabled.discard(provider)
                self.disabled.add('elsevier_abstract')
                data = self.request(provider, 'GET', url, headers=headers,
                                    params={'view': 'META', 'httpAccept': 'application/json'})
            result = elsevier_record(data)
            # An unfamiliar DOI must first prove that it belongs to the
            # ScienceDirect Article collection before requesting its abstract.
            if (view == 'META' and affinity == 'unknown' and needs_abstract and
                    'elsevier_abstract' not in self.disabled and result.get('_platform') == 'sciencedirect' and
                    doi_normalize(result.get('doi')) == doi_normalize(doi)):
                try:
                    extended = self.request(provider, 'GET', url, headers=headers,
                        params={'view': 'META_ABS', 'httpAccept': 'application/json'})
                    extended_record = elsevier_record(extended)
                    if doi_normalize(extended_record.get('doi')) == doi_normalize(doi):
                        result.update({k:v for k,v in extended_record.items() if v})
                except Exception as error:
                    if isinstance(error, MetadataHTTPError) and error.status == 403:
                        self.disabled.discard(provider)
                        self.disabled.add('elsevier_abstract')
                    result['_note'] = '已确认 ScienceDirect 收录；摘要请求失败，保留基础元数据'
            if 'elsevier_abstract' in self.disabled:
                result['_note'] = '摘要视图无权限，本批次使用基础元数据'
            return result
        raise RuntimeError('未知元数据渠道')

    def enrich(self, existing, progress=lambda msg: None, checkpoint=lambda record: None):
        record = trusted_record(existing)
        doi = doi_normalize(record.get('doi'))
        if not doi:
            return record
        record['doi'] = doi
        sources = record.setdefault('field_sources', {})
        attempts = []
        started = time.perf_counter()
        pending = enabled_providers(self.config)
        record['skipped_sources'] = []
        affinity = publisher_affinity(doi, record)
        record['metadata_strategy'] = 'standard'
        if affinity == 'elsevier' and 'elsevier' in pending:
            pending.remove('elsevier')
            pending.insert(0, 'elsevier')
            record['metadata_strategy'] = 'elsevier_direct'
        while pending:
            provider = pending.pop(0)
            if self.stop.is_set():
                break
            if not missing(record):
                break
            affinity = publisher_affinity(doi, record)
            if provider == 'elsevier' and affinity == 'other':
                record['skipped_sources'].append({'provider': provider, 'reason': '已确认为其他出版社，Article API 不适用'})
                continue
            progress(f'{doi} · {provider}')
            self.fields_needed = set(missing(record))
            self.publisher_hint = record
            provider_started = time.perf_counter()
            request_start = len(self.request_timings)
            stop_after_official = False
            try:
                incoming = self.fetch(provider, doi)
                if doi_normalize(incoming.get('doi')) != doi:
                    raise RuntimeError('未找到完全匹配的 DOI，未采纳')
                if incoming.get('_platform') == 'sciencedirect':
                    record['publisher_platform'] = 'sciencedirect'
                added = []
                for field in FIELDS:
                    value = authors_from(incoming.get(field)) if field == 'authors' else clean(incoming.get(field))
                    if field == 'year' and value and not re.fullmatch(r'(?:1[5-9]|20|21)\d{2}', value):
                        value = ''
                    if field == 'date' and record.get('year') and not value.startswith(str(record['year'])):
                        value = ''
                    if value and not record.get(field):
                        record[field] = value
                        sources[field] = provider
                        added.append(field)
                result_note = '补入 ' + '、'.join(LABELS[f].split('（')[0] for f in added) if added else '无新增字段'
                if incoming.get('_note'):
                    result_note += '；' + incoming['_note']
                if provider == 'elsevier' and incoming.get('title') and official_record_ready(record):
                    stop_after_official = True
                    record['publisher_platform'] = 'sciencedirect'
                    record['metadata_strategy'] = 'elsevier_direct'
                    result_note += '；官方核心元数据与摘要齐备，结束本篇查询'
                elif provider == 'elsevier':
                    record['metadata_strategy'] = 'elsevier_fallback'
                attempts.append({'provider': provider, 'result': result_note})
            except Exception as exc:
                # Never persist raw provider bodies/URLs/headers containing credentials.
                reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                attempts.append({'provider': provider, 'result': reason})
                if provider == 'elsevier':
                    record['metadata_strategy'] = 'elsevier_fallback'
            attempts[-1]['elapsed_seconds'] = round(time.perf_counter() - provider_started, 3)
            attempts[-1]['requests'] = copy.deepcopy(self.request_timings[request_start:])
            record['attempts'] = list(attempts)
            record['metadata_elapsed_seconds'] = round(time.perf_counter() - started, 3)
            record['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
            checkpoint(copy.deepcopy(record))
            if stop_after_official:
                break
            if publisher_affinity(doi, record) == 'elsevier' and 'elsevier' in pending:
                pending.remove('elsevier')
                pending.insert(0, 'elsevier')
                record['metadata_strategy'] = 'elsevier_direct'
        record['attempts'] = attempts
        record['metadata_elapsed_seconds'] = round(time.perf_counter() - started, 3)
        record['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        return record


def complete_metadata(doi, *, sources=None, config=None, config_path=None,
                      existing=None, on_progress=None, on_checkpoint=None,
                      stop_event=None, disabled_sources=None):
    """Complete one journal article and return a JSON-serializable dictionary.

    doi: raw DOI or https://doi.org/... URL.
    sources: optional names, e.g. ["crossref", "openalex"]. None enables ALL
        configured providers, independent of GUI checkboxes. [] disables all.
        Elsevier/ScienceDirect papers prefer the official Article API and stop
        when core citation fields and the abstract are available. Otherwise
        remaining sources follow Crossref -> OpenAlex -> Elsevier -> WOS.
        Known other publishers skip the ScienceDirect API. Unknown affiliations
        use only META until ScienceDirect coverage is confirmed. Scopus is not
        used: the currently tested key has no abstract-view entitlement.
    config: optional dictionary using the downloader's config.local.json shape.
    config_path: optional file path; default is beside this module/executable.
        Do not pass config and config_path together. No configuration is written.
    existing: optional existing metadata; filled fields remain unchanged.
    on_progress(message): optional progress callback, "DOI · provider".
    on_checkpoint(record): optional callback after each provider for persistence.
    stop_event: optional threading.Event for cooperative cancellation.
    disabled_sources: optional mutable set to share quota/permission failures
        within a batch. Ordinary callers can omit it.

    Return includes DOI and bibliographic fields, field_sources, attempts,
    updated_at, status, missing_fields, sources_used, metadata_strategy and
    skipped_sources. Network/provider errors
    are recorded in attempts; invalid arguments/configuration raise exceptions.
    This function performs no database, file-writing, CLI or GUI operations.
    """
    normalized = doi_normalize(doi)
    if not normalized:
        raise ValueError('无效 DOI')
    if config is not None and config_path is not None:
        raise ValueError('config 与 config_path 不能同时传入')
    if config is None:
        base = (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
                else Path(__file__).resolve().parent)
        path = Path(config_path).expanduser().resolve() if config_path is not None else base / 'config.local.json'
        config = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
        if config_path is not None and not path.exists():
            raise FileNotFoundError(path)
    options = copy.deepcopy(config)
    if sources is None:
        selected = list(ORDER)
    else:
        values = sources.split(',') if isinstance(sources, str) else list(sources)
        selected = [str(value).strip().lower() for value in values]
        unknown = sorted(set(selected) - set(ORDER))
        if unknown:
            raise ValueError('未知元数据来源：' + ', '.join(unknown))
    options['enabled_apis'] = selected
    options['timeout'] = options.get('timeout') or (options.get('network') or {}).get('timeout_seconds', 25)
    record = copy.deepcopy(existing or {})
    if record.get('doi') and doi_normalize(record['doi']) != normalized:
        raise ValueError('existing 中的 DOI 与请求 DOI 不一致')
    record['doi'] = normalized
    resolver = Resolver(options, stop_event)
    if disabled_sources is not None:
        resolver.disabled = disabled_sources
    def describe(item):
        item = copy.deepcopy(item)
        item['status'] = status_of(item)
        item['missing_fields'] = missing(item)
        item['sources_used'] = list(dict.fromkeys(a['provider'] for a in item.get('attempts', [])))
        return item
    try:
        result = resolver.enrich(record, on_progress or (lambda message: None),
            (lambda item: on_checkpoint(describe(item))) if on_checkpoint else (lambda item: None))
        return describe(result)
    finally:
        resolver.close()



import concurrent.futures
import csv
import difflib
import unicodedata
SEARCH_PROVIDERS = ('crossref', 'openalex')
EXPORT_FIELDS = ('doi',) + FIELDS
APP_VERSION = '2.0'


def app_directory():
    from suite_paths import APP_DIR
    return APP_DIR

def resource_directory():
    """Read-only bundled assets must never be used as the config directory."""
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)).resolve()

def resolve_config_path(path=None):
    path = Path(path).expanduser() if path is not None else Path('config.local.json')
    return (path if path.is_absolute() else app_directory() / path).resolve()

def read_config(path=None):
    resolved = resolve_config_path(path)
    if path is None and not resolved.exists():
        return {}
    data = json.loads(resolved.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or not isinstance(data.get('api_keys', {}), dict):
        raise ValueError('配置应为 JSON 对象，api_keys 应为对象')
    timeout = (data.get('network') or {}).get('timeout_seconds', 20)
    data['timeout'] = max(3, min(60, int(timeout)))
    return data

def title_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', clean(value)).casefold() if c.isalnum())

def title_similarity(query, title):
    a, b = title_key(query), title_key(title)
    return round(100 * difflib.SequenceMatcher(None, a, b).ratio(), 1) if a and b else 0.0

def journal_record(raw, provider):
    """Explicit excluded types win over apparent venue metadata."""
    kind = str(raw.get('type') or '').lower()
    if provider == 'crossref':
        return kind == 'journal-article' or (not kind and bool(raw.get('ISSN')))
    source = (raw.get('primary_location') or {}).get('source') or {}
    return (kind in ('article', 'review', 'journal-article', '')
            and not raw.get('is_paratext')
            and source.get('type') == 'journal')

def normalize_search_record(raw, provider):
    item = crossref_record({'message': raw}) if provider == 'crossref' else openalex_record(raw)
    record = {field: authors_from(item.get(field)) if field == 'authors' else clean(item.get(field))
              for field in FIELDS}
    record['doi'] = doi_normalize(item.get('doi'))
    record['type'] = 'journal-article'
    record['field_sources'] = {field: provider for field in EXPORT_FIELDS if record.get(field)}
    record['sources'] = [provider]
    record['source_ids'] = {provider: clean(raw.get('id') or raw.get('DOI'))}
    record['url'] = ('https://doi.org/' + urllib.parse.quote(record['doi'], safe='/')
                     if record['doi'] else clean(raw.get('id')))
    record['missing_fields'] = missing(record)
    if item.get('_platform'):
        record['publisher_platform'] = item['_platform']
    return record

def merge_records(records):
    """Merge identical DOIs; title-only matching needs year AND first author.

    Never merge distinct known DOIs merely because their titles are identical.
    Ambiguous no-DOI records remain separate for manual inspection.
    """
    merged = []
    # DOI-bearing rows first prevent a no-DOI bridge between different DOIs.
    for original in sorted(records, key=lambda r: not bool(r.get('doi'))):
        item = copy.deepcopy(original)
        doi = doi_normalize(item.get('doi'))
        item['doi'] = doi
        matches = [r for r in merged if doi and doi == r.get('doi')]
        if not doi:
            authors = item.get('authors') or []
            matches = [r for r in merged if title_key(item.get('title'))
                       and title_key(item['title']) == title_key(r.get('title'))
                       and item.get('year') and item['year'] == r.get('year')
                       and authors and r.get('authors')
                       and title_key(authors[0]) == title_key(r['authors'][0])]
        if len(matches) != 1:
            merged.append(item)
            continue
        target = matches[0]
        for field in EXPORT_FIELDS:
            if item.get(field) and not target.get(field):
                target[field] = item[field]
                target.setdefault('field_sources', {})[field] = item.get('field_sources', {}).get(field, '')
        target['sources'] = list(dict.fromkeys(target.get('sources', []) + item.get('sources', [])))
        target.setdefault('source_ids', {}).update(item.get('source_ids', {}))
        ranks = target.setdefault('source_ranks', {})
        for source, rank in item.get('source_ranks', {}).items():
            ranks[source] = min(ranks.get(source, rank), rank)
        target['missing_fields'] = missing(target)
    return merged

def _search_provider(provider, query, mode, limit, config, stop):
    started = time.perf_counter()
    report = {'provider': provider, 'status': 'ok', 'received': 0, 'excluded': 0}
    records = []
    resolver = Resolver(config, stop)
    keys = config.get('api_keys') or {}
    params = {}
    headers = {'Accept': 'application/json', 'User-Agent': 'LiteratureSearchGUI/1.0'}
    try:
        if stop.is_set():
            report['status'] = 'cancelled'
            return records, report
        if provider == 'crossref':
            url = 'https://api.crossref.org/works'
            if keys.get('crossref'):
                headers['Crossref-Plus-API-Token'] = 'Bearer ' + keys['crossref']
            if config.get('contact_email'):
                params['mailto'] = config['contact_email']
            if mode == 'doi':
                url += '/' + urllib.parse.quote(query, safe='')
            else:
                params.update({'query.title' if mode == 'title' else 'query': query,
                               'rows': limit, 'filter': 'type:journal-article'})
            payload = resolver.request(provider, 'GET', url, headers=headers, params=params)
            message = payload.get('message') or {}
            items = [message] if mode == 'doi' else message.get('items', [])
        else:
            url = 'https://api.openalex.org/works'
            params['api_key'] = keys['openalex']
            if mode == 'doi':
                url += '/https://doi.org/' + urllib.parse.quote(query, safe='')
            else:
                params.update({'per-page': limit, 'filter': 'type:article|review,primary_location.source.type:journal'})
                if mode == 'title':
                    # Field-specific filter is documented; punctuation must not
                    # become a comma/pipe/colon operator in the filter grammar.
                    title_query = ' '.join(re.sub(r'[^\w\s]', ' ', query).split())
                    params['filter'] += ',title.search:' + title_query
                else:
                    params['search'] = query
            payload = resolver.request(provider, 'GET', url, headers=headers, params=params)
            items = [payload] if mode == 'doi' else payload.get('results', [])
        report['received'] = len(items)
        for rank, raw in enumerate(items, 1):
            if not journal_record(raw, provider):
                report['excluded'] += 1
                continue
            record = normalize_search_record(raw, provider)
            if mode == 'doi' and record.get('doi') != query:
                report['identity_rejected'] = report.get('identity_rejected', 0) + 1
                continue
            if record.get('title') or record.get('doi'):
                record['source_ranks'] = {provider: rank}
                records.append(record)
    except Exception as error:
        if stop.is_set():
            report['status'] = 'cancelled'
        elif isinstance(error, MetadataHTTPError) and error.status == 404:
            report['status'] = 'not_found'
        else:
            report['status'] = 'error'
            # Requests exception strings can contain URLs with API keys.
            report['error'] = str(error) if isinstance(error, MetadataHTTPError) else type(error).__name__
    finally:
        resolver.close()
        report['elapsed_seconds'] = round(time.perf_counter() - started, 2)
    return records, report

def search_metadata(query, *, mode='auto', limit=20, config=None, sources=None,
                    stop_event=None, on_progress=None, on_partial=None):
    """Search without a GUI. Returns records + source reports + pool counts.

    modes: auto (DOI, otherwise title), title, keyword, doi.
    limit: final result ceiling, 1..100, each provider requests that many rows.
    config: downloader config dict; when omitted read adjacent config.local.json.
    sources: search sources only (crossref/openalex); None uses available ones.
    Callbacks run on the caller thread, not Tk's main thread.
    """
    query = str(query or '').strip()
    if not query:
        raise ValueError('请输入标题、关键词或 DOI')
    if mode not in ('auto', 'title', 'keyword', 'doi'):
        raise ValueError('未知检索模式')
    if not 1 <= int(limit) <= 100:
        raise ValueError('结果上限应为 1–100')
    limit = int(limit)
    if mode == 'auto':
        mode = 'doi' if doi_normalize(query) else 'title'
    if mode == 'doi':
        query = doi_normalize(query)
        if not query:
            raise ValueError('DOI 格式无效，请输入 DOI 或 doi.org 链接')
    config = copy.deepcopy(config if config is not None else read_config())
    config['timeout'] = max(3, min(60, int(config.get('timeout') or (config.get('network') or {}).get('timeout_seconds', 20))))
    selected = list(SEARCH_PROVIDERS if sources is None else sources)
    if set(selected) - set(SEARCH_PROVIDERS):
        raise ValueError('搜索来源仅支持 crossref/openalex')
    selected = list(dict.fromkeys(selected))
    stop = stop_event if stop_event is not None else threading.Event()
    progress = on_progress or (lambda message: None)
    records, reports = [], []
    active = []
    for source in selected:
        if provider_available(config, source):
            active.append(source)
        else:
            reports.append({'provider': source, 'status': 'skipped', 'error': '未配置 Key', 'excluded': 0})
    def snapshot():
        # Stable source priority avoids changing field attribution with network timing.
        ordered = sorted(records, key=lambda r: SEARCH_PROVIDERS.index(r['sources'][0]))
        merged = merge_records(ordered)
        for record in merged:
            record['title_similarity'] = title_similarity(query, record.get('title')) if mode == 'title' else None
            record['rank_score'] = sum(1 / (60 + rank) for rank in record.get('source_ranks', {}).values())
        merged.sort(key=lambda r: (r.get('title_similarity') or 0, r['rank_score']), reverse=True)
        return {'query': query, 'mode': mode, 'records': merged[:limit], 'candidate_count': len(merged),
                'count': min(len(merged), limit), 'excluded_count': sum(r.get('excluded', 0) for r in reports),
                'source_reports': copy.deepcopy(reports), 'cancelled': stop.is_set()}
    if active and not stop.is_set():
        progress('正在查询 ' + ' / '.join(PROVIDER_LABELS[s] for s in active))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(active)) as pool:
            futures = [pool.submit(_search_provider, p, query, mode, limit, config, stop) for p in active]
            for future in concurrent.futures.as_completed(futures):
                rows, report = future.result()
                records.extend(rows)
                reports.append(report)
                progress(f"{PROVIDER_LABELS[report['provider']]}：{len(rows)} 篇期刊记录，{report['status']}")
                if on_partial:
                    on_partial(snapshot())
    return snapshot()

def export_records(path, records):
    """JSON list uses the downloader's field names; CSV is Excel UTF-8 BOM."""
    path = Path(path)
    if path.suffix.lower() == '.json':
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    elif path.suffix.lower() == '.csv':
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=EXPORT_FIELDS)
            writer.writeheader()
            for record in records:
                row = {key: '; '.join(record.get(key) or []) if key == 'authors' else clean(record.get(key))
                       for key in EXPORT_FIELDS}
                # Treat remote metadata as text when opened in Excel.
                writer.writerow({k: "'" + v if v.startswith(('=', '+', '-', '@')) else v for k, v in row.items()})
    elif path.suffix.lower() == '.ris':
        lines = []
        for record in records:
            lines.append('TY  - JOUR')
            for key, tag in [('title', 'TI'), ('journal', 'JO'), ('journal_abbreviation', 'J2'),
                             ('year', 'PY'), ('date', 'DA'), ('volume', 'VL'), ('issue', 'IS'),
                             ('doi', 'DO'), ('issn', 'SN'), ('abstract', 'AB'), ('publisher', 'PB'),
                             ('language', 'LA'), ('url', 'UR')]:
                if record.get(key):
                    lines.append(f'{tag}  - {clean(record[key])}')
            lines.extend('AU  - ' + clean(author) for author in record.get('authors') or [])
            pages = clean(record.get('pages'))
            article = clean(record.get('article_number'))
            if pages:
                parts = re.split(r'[-–—]', pages, maxsplit=1)
                lines.append('SP  - ' + parts[0])
                if len(parts) > 1:
                    lines.append('EP  - ' + parts[1])
            elif article:
                lines.append('SP  - ' + article)
            if article:
                lines.append('N1  - Article number: ' + article)
            lines.extend(['ER  - ', ''])
        path.write_text('\n'.join(lines), encoding='utf-8')
    else:
        raise ValueError('导出格式应为 .json、.csv 或 .ris')

