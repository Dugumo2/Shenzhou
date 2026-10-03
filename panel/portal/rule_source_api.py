"""管理员文件来源查询、差异确认和本地候选保存接口。"""
import json
import uuid

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.core.paginator import EmptyPage, Paginator
from django.db import OperationalError, transaction
from django.db.models import F, Q

from . import api
from .models import RuleSource, RuleSourceVersion
from .rule_sources import (INPUT_KEYS, MAX_BYTES, MAX_RULES, MESSAGE, SourceError, commit_source,
                           preview_source, source_item, storage_valid,
                           strict_json, verified_document, version_item)


HISTORY_LIMIT = 100
LIMITATIONS = ['仅支持严格 JSON schema_version=1 的文件来源。',
               'HTTPS 获取、来源覆盖补丁、组合顺序、方案绑定与发布尚未接入。',
               '条目重叠不表示最终命中；本片不会修改自建规则。']


def _failure(exc):
    response = api.error(exc.code, exc.message, exc.status, exc.fields)
    body = json.loads(response.content)
    body['error'].update(field_errors=exc.fields, conflicts=exc.conflicts)
    response.content = json.dumps(body, ensure_ascii=False)
    return response


def _context():
    return {'read_only': bool(settings.PANEL_LIVE) or not storage_valid(),
            'scope': 'local_candidate', 'publish_state': 'unknown', 'client_apply_state': 'unknown',
            'limits': {'max_bytes': MAX_BYTES, 'max_rules': MAX_RULES},
            'limitations': LIMITATIONS, 'message': MESSAGE}


def _payload(request, extra=()):
    if request.content_type != 'application/json':
        raise SourceError('unsupported_media_type', '请使用 JSON 请求。', 415)
    try:
        raw = request.body
    except RequestDataTooBig:
        raise SourceError('document_too_large', '请求超过上传大小限制。') from None
    # JSON 字符串转义可能把一个字节展开为六个字符；实际文件仍单独受 256 KiB 限制。
    if len(raw) > MAX_BYTES * 6 + 8192:
        raise SourceError('document_too_large', '请求超过上传大小限制。')
    data = strict_json(raw)
    if type(data) is not dict or set(data) != INPUT_KEYS | set(extra):
        raise SourceError('invalid_fields', '请求字段不完整或含有不支持的字段。')
    return data


def _paginate(items, options):
    paginator = Paginator(items, options[1])
    try:
        page = paginator.page(options[0])
    except EmptyPage:
        raise SourceError('page_not_found', '此页不存在，请返回前一页。', 404) from None
    return list(page), {'page': page.number, 'page_size': options[1], 'total': paginator.count,
                        'pages': paginator.num_pages, 'has_next': page.has_next(),
                        'has_previous': page.has_previous()}


@api.endpoint(staff=True)
def sources(request):
    options, failure = api.page_options(request)
    if failure is not None:
        return failure
    q = request.GET.get('q', '').strip()
    if len(q) > 253:
        return api.error('invalid_filter', '搜索内容过长。', 422)
    try:
        with transaction.atomic():
            query = RuleSource.objects.order_by('-updated_at', 'pk')
            if q:
                match = Q(name__icontains=q)
                try:
                    match |= Q(public_id=uuid.UUID(q))
                except ValueError:
                    pass
                query = query.filter(match)
            rows, pagination = _paginate(query, options)
            versions = {version.source_id: version for version in RuleSourceVersion.objects.filter(
                source__in=rows, revision=F('source__revision'))}
            for row in rows:
                if row.pk not in versions:
                    raise SourceError('invalid_existing_source', '来源当前版本缺失，请核查。', 409)
                verified_document(versions[row.pk])
            return api.success({'items': [source_item(row, versions.get(row.pk)) for row in rows],
                                'pagination': pagination, **_context()})
    except SourceError as exc:
        return _failure(exc)
    except OperationalError:
        return _failure(SourceError('database_busy', '候选库忙碌，请稍后刷新。', 409))


@api.endpoint(staff=True)
def source_detail(request, source_id):
    options, failure = api.page_options(request)
    if failure is not None:
        return failure
    q = request.GET.get('q', '').strip().lower()
    if len(q) > 253:
        return api.error('invalid_filter', '搜索内容过长。', 422)
    try:
        with transaction.atomic():
            try:
                public_id = uuid.UUID(str(source_id))
            except ValueError:
                raise SourceError('not_found', '来源不存在。', 404) from None
            source = RuleSource.objects.filter(public_id=public_id).first()
            if source is None:
                raise SourceError('not_found', '来源不存在。', 404)
            revision_text = request.GET.get('version', str(source.revision))
            if not revision_text.isascii() or not revision_text.isdecimal() or len(revision_text) > 10:
                raise SourceError('invalid_fields', '版本参数必须是有效修订号。')
            selected = source.versions.filter(revision=int(revision_text)).first()
            latest = selected if int(revision_text) == source.revision else source.versions.filter(
                revision=source.revision).first()
            if selected is None:
                raise SourceError('not_found', '来源版本不存在。', 404)
            try:
                entries = verified_document(selected)['rules']
                if latest is None:
                    raise SourceError('invalid_existing_source', '来源当前版本缺失，请核查。', 409)
                verified_document(latest)
            except SourceError:
                raise SourceError('invalid_existing_source', '来源版本无效，请核查；此次读取未改写。', 409) from None
            if q:
                entries = [row for row in entries if any(q in str(row[key]).lower()
                           for key in ('action', 'kind', 'value', 'scope_domain'))]
            items, pagination = _paginate(entries, options)
            history = list(source.versions.order_by('-revision')[:HISTORY_LIMIT + 1])
            for version in history[:HISTORY_LIMIT]:
                verified_document(version)
            return api.success({'item': source_item(source, latest), 'version': version_item(selected),
                'items': items, 'pagination': pagination,
                'history': [version_item(version) for version in history[:HISTORY_LIMIT]],
                'history_truncated': len(history) > HISTORY_LIMIT, 'history_limit': HISTORY_LIMIT,
                **_context()})
    except SourceError as exc:
        return _failure(exc)
    except OperationalError:
        return _failure(SourceError('database_busy', '候选库忙碌，请稍后刷新。', 409))


@api.endpoint(methods=('POST',), staff=True)
def preview(request):
    try:
        return api.success(preview_source(request.user, _payload(request)))
    except SourceError as exc:
        return _failure(exc)


@api.endpoint(methods=('POST',), staff=True)
def commit(request):
    try:
        result, status = commit_source(request.user, _payload(request, ('preview_token', 'idempotency_key')))
        return api.success(result, status=status)
    except SourceError as exc:
        return _failure(exc)
