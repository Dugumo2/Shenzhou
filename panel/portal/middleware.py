"""页面与订阅响应禁止缓存；不加载第三方脚本。"""
class ResponsePolicy:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response['Cache-Control'] = 'no-store, private'
        response['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.path.startswith('/app/'):
            # Element Plus 的定位/宽度使用动态样式；仅允许样式属性，脚本仍限同源。
            response['Content-Security-Policy'] += "; style-src-attr 'unsafe-inline'"
        response['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        return response
