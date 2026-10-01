"""公网使用标准框架安全功能；生产必须显式提供配置。"""
import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
PANEL_LIVE = os.environ.get('PANEL_LIVE') == '1'
DEBUG = False
SECRET_KEY = os.environ.get('PANEL_SECRET_KEY', '')
if PANEL_LIVE and len(SECRET_KEY) < 50:
    raise ImproperlyConfigured('生产需要独立的高强度 PANEL_SECRET_KEY')
if not SECRET_KEY:
    # 仅限测试候选环境，绝不能携带到生产。
    SECRET_KEY = 'local-candidate-only-not-for-public-use-' * 2
ALLOWED_HOSTS = os.environ.get('PANEL_HOSTS', 'localhost,127.0.0.1,testserver').split(',')
if PANEL_LIVE and (set(ALLOWED_HOSTS) & {'*', 'testserver', 'localhost', '127.0.0.1'}):
    raise ImproperlyConfigured('生产必须指定唯一或明确的域名清单')
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions',
                  'django.contrib.messages', 'django.contrib.staticfiles', 'portal']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware',
              'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
              'django.contrib.auth.middleware.AuthenticationMiddleware', 'django.contrib.messages.middleware.MessageMiddleware',
              'django.middleware.clickjacking.XFrameOptionsMiddleware', 'portal.middleware.ResponsePolicy']
ROOT_URLCONF = 'megabox.urls'
CSRF_FAILURE_VIEW = 'portal.api.csrf_failure'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'APP_DIRS': True,
              'OPTIONS': {'context_processors': ['django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages',
                'portal.context.environment']}}]
WSGI_APPLICATION = 'megabox.wsgi.application'
DATA_ROOT = Path(os.environ.get('PANEL_DATA_ROOT', str(BASE_DIR / 'var')))
DATA_ROOT.mkdir(parents=True, exist_ok=True)
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': DATA_ROOT / 'panel.sqlite3',
                          'OPTIONS': {'timeout': 20, 'transaction_mode': 'IMMEDIATE'}}}
PASSWORD_HASHERS = ['django.contrib.auth.hashers.Argon2PasswordHasher',
                    'django.contrib.auth.hashers.PBKDF2PasswordHasher']
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'}]
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'collected-static'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/login/'
SESSION_COOKIE_NAME = 'megabox_session'
SESSION_COOKIE_SECURE = PANEL_LIVE
CSRF_COOKIE_SECURE = PANEL_LIVE
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Strict'
SESSION_COOKIE_AGE = 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SECURE_SSL_REDIRECT = PANEL_LIVE
SECURE_HSTS_SECONDS = 86400 if PANEL_LIVE else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
# 同源表单需要发送来源供 CSRF 校验；跨站请求仍不泄露 Referer。
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
DATA_UPLOAD_MAX_MEMORY_SIZE = 65536
FILE_UPLOAD_MAX_MEMORY_SIZE = 0
# 只信任部署时明确隔离的反向代理，不能无条件信任公网转发头。
if os.environ.get('PANEL_TRUST_PROXY') == '1':
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
CSRF_TRUSTED_ORIGINS = [x.strip() for x in os.environ.get('PANEL_CSRF_ORIGINS', '').split(',') if x.strip()]
ARTIFACT_ROOT = Path(os.environ.get('PANEL_ARTIFACT_ROOT', str(DATA_ROOT / 'artifacts')))
LEGACY_LINKS_PATH = Path(os.environ['PANEL_LEGACY_LINKS_PATH']) if os.environ.get('PANEL_LEGACY_LINKS_PATH') else None
ROUTING_POLICY_PATH = Path(os.environ.get('PANEL_ROUTING_POLICY_PATH', str(DATA_ROOT / 'private' / 'routing-policy.json')))
ROUTING_STATUS_PATH = Path(os.environ.get('PANEL_ROUTING_STATUS_PATH', str(DATA_ROOT / 'private' / 'routing-publish-status.json')))
SNAPSHOT_PATH = DATA_ROOT / 'status.json'
OPERATOR_ENABLED = os.environ.get('PANEL_OPERATOR_ENABLED') == '1'
if OPERATOR_ENABLED and not PANEL_LIVE:
    raise ImproperlyConfigured('本地候选禁止连接生产执行器')

# 新工作区先独立验收；生产显式开启，避免迁移前改变现有交付路径。
WORKSPACE_V2 = os.environ.get('PANEL_WORKSPACE_V2') == '1'
SITE_BRAND = os.environ.get('PANEL_SITE_BRAND', '神舟云')[:60]
FRONTEND_ENABLED = os.environ.get('PANEL_FRONTEND_ENABLED') == '1'
CANDIDATE_DEMO_DATA = not PANEL_LIVE and os.environ.get('PANEL_CANDIDATE_DEMO_DATA') == '1'
MONITOR_URL = os.environ.get('PANEL_MONITOR_URL', '')
if MONITOR_URL:
    from urllib.parse import urlsplit
    monitor_target = urlsplit(MONITOR_URL)
    if (monitor_target.scheme != 'https' or not monitor_target.hostname or monitor_target.username
            or monitor_target.password or monitor_target.query or monitor_target.fragment):
        raise ImproperlyConfigured('监控入口必须是无凭据、无查询参数的 HTTPS 固定地址')
