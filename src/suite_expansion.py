"""Bounded one-hop expansion of references and citing journal papers."""
import copy
import re
import threading
import urllib.parse
import suite_search as search


def expand_seeds(seeds, *, limit=20, direction='both', config=None, stop_event=None, on_progress=None):
    if not 1 <= int(limit) <= 100:
        raise ValueError('拓展上限应为 1–100')
    if direction not in ('both', 'references', 'citations'):
        raise ValueError('未知拓展方向')
    if not seeds:
        raise ValueError('请至少提供一篇种子文献')
    stop = stop_event or threading.Event()
    progress = on_progress or (lambda msg: None)
    config = copy.deepcopy(config if config is not None else search.read_config())
    keys = config.get('api_keys') or {}
    allowed = config.get('enabled_apis', search.ORDER)
    allow_crossref = 'crossref' in allowed
    if 'openalex' not in allowed:
        keys = {k:v for k,v in keys.items() if k != 'openalex'}
    if not allow_crossref and not keys.get('openalex'):
        raise ValueError('种子拓展需要启用 Crossref 或已配置 Key 的 OpenAlex')
    resolver = search.Resolver(config, stop)
    reports, seed_records, groups = [], [], []
    excluded = 0
    seen_seeds = set()
    headers = {'Accept':'application/json', 'User-Agent':'LiteratureSuite/1.0'}

    def request(provider, url, params=None):
        if stop.is_set():
            raise RuntimeError('已停止')
        if provider in resolver.disabled:
            raise RuntimeError('本批次已暂停该来源')
        parameters = dict(params or {})
        h = dict(headers)
        if provider == 'openalex':
            parameters['api_key'] = keys['openalex']
        else:
            if config.get('contact_email'): parameters['mailto'] = config['contact_email']
            if keys.get('crossref'): h['Crossref-Plus-API-Token'] = 'Bearer ' + keys['crossref']
        return resolver.request(provider, 'GET', url, headers=h, params=parameters)

    def report(seed, provider, relation, error=None, count=0):
        item={'seed':seed, 'provider':provider, 'relation':relation, 'count':count, 'status':'ok' if error is None else 'error'}
        if error is not None:
            item['error']=str(error) if isinstance(error, search.MetadataHTTPError) else type(error).__name__
        reports.append(item)

    def add(raw, provider, seed_id, relation, target):
        nonlocal excluded
        if not search.journal_record(raw, provider):
            excluded += 1
            return
        record=search.normalize_search_record(raw, provider)
        record['relation']=[relation]
        record['seed_dois']=[seed_id]
        record['hop']=1
        target.append(record)

    try:
        for position, supplied in enumerate(seeds,1):
            if stop.is_set(): break
            supplied = supplied if isinstance(supplied,dict) else {'doi':search.doi_normalize(supplied),'title':str(supplied)}
            doi=search.doi_normalize(supplied.get('doi'))
            oa_id=str((supplied.get('source_ids') or {}).get('openalex') or '').rsplit('/',1)[-1]
            if not re.fullmatch(r'W\d+',oa_id): oa_id=''
            if not doi and not oa_id:
                resolved=search.search_metadata(supplied.get('title',''),mode='title',limit=10,config=config,stop_event=stop)
                exact=[r for r in resolved['records'] if search.title_key(r['title'])==search.title_key(supplied.get('title'))]
                if len(exact)!=1:
                    reports.append({'seed':supplied.get('title',''),'provider':'lookup','relation':'seed','status':'error','error':'标题未唯一匹配，请提供 DOI'})
                    continue
                supplied=exact[0];doi=supplied.get('doi','');oa_id=(supplied.get('source_ids') or {}).get('openalex','').rsplit('/',1)[-1]
            identity=doi or oa_id
            if identity in seen_seeds:continue
            seen_seeds.add(identity)
            progress(f'拓展种子 {position}/{len(seeds)}：{identity}')
            raw_oa=None
            raw_cr=None
            verified=False
            known_nonjournal=False
            if keys.get('openalex'):
                try:
                    identifier='https://doi.org/'+urllib.parse.quote(doi,safe='') if doi else oa_id
                    raw_oa=request('openalex','https://api.openalex.org/works/'+identifier)
                    if doi and search.doi_normalize(raw_oa.get('doi'))!=doi: raise ValueError('DOI mismatch')
                    known_nonjournal=not search.journal_record(raw_oa,'openalex')
                    verified=not known_nonjournal
                    if verified:
                        oa_id=str(raw_oa.get('id') or '').rsplit('/',1)[-1]
                        seed_records.append(search.normalize_search_record(raw_oa,'openalex'))
                    report(identity,'openalex','seed',count=int(verified))
                except Exception as error:report(identity,'openalex','seed',error)
            if doi and allow_crossref and not verified and not known_nonjournal and not stop.is_set():
                try:
                    raw_cr=(request('crossref','https://api.crossref.org/works/'+urllib.parse.quote(doi,safe='')).get('message') or {})
                    if search.doi_normalize(raw_cr.get('DOI'))!=doi:raise ValueError('DOI mismatch')
                    verified=search.journal_record(raw_cr,'crossref')
                    known_nonjournal=not verified
                    if verified:seed_records.append(search.normalize_search_record(raw_cr,'crossref'))
                    report(identity,'crossref','seed',count=int(verified))
                except Exception as error:report(identity,'crossref','seed',error)
            if not verified:
                excluded+=int(known_nonjournal)
                continue
            references, citations = [], []
            if direction in ('both','references') and raw_oa and raw_oa.get('referenced_works'):
                try:
                    ids=list(dict.fromkeys(str(x).rsplit('/',1)[-1] for x in raw_oa['referenced_works']))[:min(200,int(limit)*2)]
                    for offset in range(0,len(ids),50):
                        if stop.is_set():break
                        payload=request('openalex','https://api.openalex.org/works',{'filter':'openalex_id:'+'|'.join(ids[offset:offset+50]),'per-page':50})
                        for raw in payload.get('results') or []:add(raw,'openalex',identity,'参考文献',references)
                    report(identity,'openalex','references',count=len(references))
                except Exception as error:report(identity,'openalex','references',error)
            if direction in ('both','references') and not references and doi and allow_crossref and not stop.is_set():
                try:
                    if raw_cr is None:raw_cr=(request('crossref','https://api.crossref.org/works/'+urllib.parse.quote(doi,safe='')).get('message') or {})
                    dois=list(dict.fromkeys(search.doi_normalize(r.get('DOI')) for r in raw_cr.get('reference') or []))
                    for ref_doi in [d for d in dois if d][:min(100,int(limit)*2)]:
                        if stop.is_set():break
                        try:
                            raw=(request('crossref','https://api.crossref.org/works/'+urllib.parse.quote(ref_doi,safe='')).get('message') or {})
                            if search.doi_normalize(raw.get('DOI'))==ref_doi:add(raw,'crossref',identity,'参考文献',references)
                        except Exception as error:
                            report(identity,'crossref','reference_lookup',error)
                            if 'crossref' in resolver.disabled:break
                        if len(references)>=limit:break
                    report(identity,'crossref','references',count=len(references))
                except Exception as error:report(identity,'crossref','references',error)
            if direction in ('both','citations') and keys.get('openalex') and oa_id and not stop.is_set():
                try:
                    cursor='*'
                    for _ in range(3):
                        payload=request('openalex','https://api.openalex.org/works',{'filter':f'cites:{oa_id},type:article|review,primary_location.source.type:journal','per-page':min(100,int(limit)),'cursor':cursor})
                        for raw in payload.get('results') or []:add(raw,'openalex',identity,'施引文献',citations)
                        cursor=(payload.get('meta') or {}).get('next_cursor')
                        if not cursor or not payload.get('results') or len(citations)>=limit or stop.is_set():break
                    report(identity,'openalex','citations',count=len(citations))
                except Exception as error:report(identity,'openalex','citations',error)
            elif direction in ('both','citations') and not keys.get('openalex'):
                reports.append({'seed':identity,'provider':'openalex','relation':'citations','status':'skipped','error':'未配置 OpenAlex Key，无法查询施引文献'})
            # Interleave directions to avoid returning only references for both.
            combined=[]
            for i in range(max(len(references),len(citations),0)):
                if i<len(references):combined.append(references[i])
                if i<len(citations):combined.append(citations[i])
            groups.append(combined)
    finally:resolver.close()
    seed_dois={r.get('doi') for r in seed_records if r.get('doi')}
    seed_ids={(r.get('source_ids') or {}).get('openalex') for r in seed_records}
    ordered=[]
    for i in range(max([len(g) for g in groups] or [0])):
        for group in groups:
            if i<len(group):ordered.append(group[i])
    unique={}
    for record in ordered:
        oid=(record.get('source_ids') or {}).get('openalex')
        if record.get('doi') in seed_dois or (oid and oid in seed_ids):continue
        key=record.get('doi') or oid or search.title_key(record.get('title'))
        if key in unique:
            old=unique[key]
            merged=search.merge_records([old,record])[0]
            for field in ('relation','seed_dois'):
                merged[field]=list(dict.fromkeys(old.get(field,[])+record.get(field,[])))
            unique[key]=merged
        else:unique[key]=record
    records=list(unique.values())[:int(limit)]
    return {'mode':'expand','records':records,'count':len(records),'candidate_count':len(unique),'excluded_count':excluded,
            'seeds':seed_records,'direction':direction,'limit':int(limit),'source_reports':reports,'cancelled':stop.is_set()}
