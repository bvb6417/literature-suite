"""Public CLI for the same entry file that launches the GUI."""
import argparse
import json
import sys
from pathlib import Path
from suite_paths import APP_DIR, absolute_path
import suite_search as search
from suite_expansion import expand_seeds
from suite_settings import search_limit,pdf_directory


def positive_limit(value):
    value=int(value)
    if not 1<=value<=100:raise argparse.ArgumentTypeError('上限应为 1–100')
    return value


def parser():
    root=argparse.ArgumentParser(description='文献工作台：不传参数打开 GUI；传入子命令使用 CLI。')
    sub=root.add_subparsers(dest='command',required=True)
    sub.add_parser('gui',help='打开统一 GUI')
    for name,label in [('keyword','关键词检索'),('doi','精确 DOI 检索'),('search','标题或关键词检索')]:
        p=sub.add_parser(name,help=label)
        p.add_argument('query')
        p.add_argument('--limit',type=positive_limit,default=None,help='默认使用全局配置中的检索上限')
        if name=='search':p.add_argument('--mode',choices=['title','keyword','auto'],default='title')
        common(p)
    p=sub.add_parser('expand',help='多种子一跳参考文献/施引文献拓展')
    p.add_argument('seeds',nargs='+',help='DOI 或可唯一匹配的完整标题')
    p.add_argument('--limit',type=positive_limit,default=None,help='默认使用全局配置中的检索上限')
    p.add_argument('--direction',choices=['both','references','citations'],default='both')
    common(p)
    p=sub.add_parser('enrich',help='补全文献元数据，已有字段保持不变')
    p.add_argument('dois',nargs='*')
    p.add_argument('--input',type=Path,help='JSON 列表或包含 records 的对象')
    common(p)
    # Real options are delegated intact to the bundled download engine.
    sub.add_parser('download',help='下载 DOI；download --help 查看下载参数',add_help=False)
    sub.add_parser('check',help='检查下载渠道；输出脱敏状态',add_help=False)
    return root


def common(p):
    p.add_argument('--config',type=Path,default=APP_DIR/'config.local.json')
    p.add_argument('--sources',help='逗号分隔来源；检索支持 crossref,openalex')
    p.add_argument('--output','-o',type=Path,help='保存 JSON / CSV / RIS；stdout 始终返回 JSON')
    p.add_argument('--json',action='store_true',help='兼容选项；默认已为 JSON')


def source_names(value,allowed):
    if value is None:return None
    selected=list(dict.fromkeys(v.strip().lower() for v in value.split(',') if v.strip()))
    if not selected or set(selected)-set(allowed):raise ValueError('不支持的来源：'+value)
    return selected


def emit(data,output=None):
    if output:
        path=absolute_path(output)
        if path.suffix.lower() in ('.csv','.ris'):
            search.export_records(path,data.get('records',[]))
        else:
            path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(data,ensure_ascii=False,indent=2))


def enrich_records(records,config,sources=None):
    result=[];errors=[];excluded=0
    for original in records:
        record=dict(original) if isinstance(original,dict) else {'doi':original}
        doi=search.doi_normalize(record.get('doi'))
        if not doi:
            errors.append({'doi':str(record.get('doi','')),'error':'无效 DOI'});continue
        kind=record.get('type','')
        if kind and kind not in ('journal-article','article','review'):
            excluded+=1;continue
        if not kind and not (record.get('journal') or record.get('issn')):
            lookup=search.search_metadata(doi,mode='doi',limit=1,config=config)
            if not lookup['records']:
                errors.append({'doi':doi,'error':'未查到可确认的期刊论文','source_reports':lookup['source_reports']});continue
            resolved=lookup['records'][0]
            for key,value in record.items():
                if value:resolved[key]=value
            record=resolved
        completed=search.complete_metadata(doi,config=config,existing=record,sources=sources)
        result.append(completed)
    return {'ok':not errors,'records':result,'count':len(result),'excluded_count':excluded,'errors':errors}


def normalize_kernel_paths(arguments):
    values=list(arguments)
    for flag in ('--config','--output-dir'):
        if flag in values:
            i=values.index(flag)
            if i+1<len(values):values[i+1]=str(absolute_path(values[i+1]))
        for i,value in enumerate(values):
            if value.startswith(flag+'='):values[i]=flag+'='+str(absolute_path(value.split('=',1)[1]))
    if values and values[0]=='download' and not any(v=='--output-dir' or v.startswith('--output-dir=') for v in values):
        config_path=APP_DIR/'config.local.json'
        if '--config' in values:
            position=values.index('--config')
            if position+1<len(values):config_path=Path(values[position+1])
        for value in values:
            if value.startswith('--config='):config_path=absolute_path(value.split('=',1)[1])
        config=search.read_config(config_path) if config_path.exists() else {}
        values.extend(['--output-dir',str(pdf_directory(config))])
    return values


def main(argv):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    if argv and argv[0]=='_metadata-worker':
        worker=argparse.ArgumentParser()
        worker.add_argument('--input',type=Path,required=True)
        worker.add_argument('--config',type=Path,required=True)
        worker.add_argument('--sources',default='crossref,openalex,elsevier,wos')
        worker.add_argument('--disabled',default='')
        args=worker.parse_args(argv[1:])
        disabled=set(filter(None,args.disabled.split(',')))
        def event(kind,**data):print(json.dumps({'event':kind,**data},ensure_ascii=False),flush=True)
        try:
            record=json.loads(args.input.read_text(encoding='utf8'))
            config=search.read_config(args.config) if args.config.exists() else {}
            result=search.complete_metadata(record['doi'],config=config,existing=record,
                sources=list(filter(None,args.sources.split(','))),disabled_sources=disabled,
                on_progress=lambda msg:event('progress',message=msg),
                on_checkpoint=lambda row:event('checkpoint',record=row,disabled=list(disabled)))
            event('complete',record=result,disabled=list(disabled))
            return 0
        except Exception as error:
            event('error',message=type(error).__name__)
            return 2
    if argv and argv[0] in ('download','check','_download-kernel'):
        from literature_download_cli import main as kernel_main
        args=argv[1:] if argv[0]=='_download-kernel' else list(argv)
        if args and args[0] in ('download','check') and not any(v in args for v in ('--json','--json-lines','--help','-h')):args.append('--json')
        try:
            return kernel_main(normalize_kernel_paths(args))
        except Exception as error:
            emit({'ok':False,'error':type(error).__name__})
            return 2
    args=parser().parse_args(argv)
    if args.command=='gui':
        from suite_gui import launch
        return launch()
    try:
        path=absolute_path(args.config)
        config=search.read_config(path) if path.exists() else {}
        if hasattr(args,'limit') and args.limit is None:args.limit=search_limit(config)
        sources=source_names(args.sources,search.ORDER if args.command=='enrich' else search.SEARCH_PROVIDERS)
        if args.command in ('search','keyword','doi'):
            mode={'keyword':'keyword','doi':'doi'}.get(args.command,getattr(args,'mode','title'))
            data=search.search_metadata(args.query,mode=mode,limit=args.limit,config=config,sources=sources)
            if mode=='doi' and data['records']:
                data['records']=[search.complete_metadata(r['doi'],config=config,existing=r,sources=sources) for r in data['records']]
            data['ok']=any(r['status']=='ok' for r in data['source_reports'])
        elif args.command=='expand':
            if sources is not None:config['enabled_apis']=sources
            data=expand_seeds(args.seeds,limit=args.limit,direction=args.direction,config=config)
            data['ok']=bool(data['seeds']) and (bool(data['records']) or not any(r['status']=='error' for r in data['source_reports']))
        else:
            records=[{'doi':doi} for doi in args.dois]
            if args.input:
                payload=json.loads(absolute_path(args.input).read_text(encoding='utf-8-sig'))
                rows=payload.get('records') if isinstance(payload,dict) else payload
                if not isinstance(rows,list):raise ValueError('输入 JSON 应为列表或包含 records 列表')
                records.extend(rows)
            if not records:raise ValueError('请提供 DOI 或 --input JSON 文件')
            data=enrich_records(records,config,sources)
        emit(data,args.output)
        return 0 if data.get('ok') else 1
    except Exception as error:
        message=str(error) if isinstance(error,(ValueError,FileNotFoundError)) else type(error).__name__
        emit({'ok':False,'error':message})
        return 2
