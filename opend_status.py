"""Thread-safe, bounded OpenD diagnostics for the browser (not a log viewer)."""
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import re
from threading import Lock, local


def error_message(error):
    message = str(error)
    message = re.sub(r'-----BEGIN .*?PRIVATE KEY-----.*?-----END .*?PRIVATE KEY-----',
                     '[private key redacted]', message, flags=re.S)
    message = re.sub(r'(?i)\b(password|password_md5|token|login_account|acc_id)\s*[:=]\s*[^\s,;]+',
                     r'\1=[redacted]', message)
    return message[:1200]


def error_hint(message):
    lower = message.lower()
    if 'check sha' in lower or 'decrypt' in lower:
        return '加密握手失败：请配置 --rsa_private_key，并确认客户端与 OpenD 使用同一份 RSA 私钥。'
    if 'must be encrypted' in lower or 'rsa' in lower or 'private key' in lower:
        return '请检查 OpenD 的 rsa_private_key 和客户端 --rsa_private_key / FUTU_RSA_PRIVATE_KEY 配置。'
    if 'login' in lower or 'logined' in lower:
        return '请检查 OpenD 是否已登录，并完成设备或短信验证。'
    if any(word in lower for word in ('connect', 'timeout', 'timed out', 'refused', 'disconnect')):
        return '请检查 OpenD 是否运行、监听地址和端口，以及防火墙是否允许连接。'
    return '系统会自动重试；请检查 OpenD 状态和上述错误原因。'


class OpenDStatus(logging.Handler):
    def __init__(self, host, ports):
        super().__init__(logging.WARNING)
        self.host = host
        self.ports = list(ports)
        self._state_lock = Lock()
        self._local = local()
        self._errors = {}
        self._sequence = 0
        self._loggers = []

    def capture_logs(self):
        # FTFileLog also receives SDK warnings when console logging is disabled.
        for name in ('FTFileLog', 'opdash.core', 'positions'):
            logger = logging.getLogger(name)
            logger.addHandler(self)
            self._loggers.append(logger)

    def detach(self):
        for logger in self._loggers:
            logger.removeHandler(self)
        self._loggers.clear()

    def emit(self, record):
        operation = getattr(self._local, 'operation', None)
        if operation is None:
            return
        message = record.getMessage()
        if record.name == 'FTFileLog' and not any(
                text in message.lower() for text in
                ('fail', 'error', 'timeout', 'timed out', 'disconnect')):
            return
        self.report(*operation, message)

    def report(self, scope, port, error):
        message = error_message(error)
        with self._state_lock:
            self._sequence += 1
            self._errors[(scope, port)] = {
                'scope': scope, 'host': self.host, 'port': port,
                'message': message, 'hint': error_hint(message),
                'occurred_at': datetime.now(timezone.utc).isoformat(),
                '_sequence': self._sequence,
            }

    def clear(self, scope, port):
        with self._state_lock:
            self._errors.pop((scope, port), None)

    def errors(self):
        with self._state_lock:
            return [{key: value for key, value in error.items() if key != '_sequence'}
                    for error in self._errors.values()]

    @contextmanager
    def operation(self, scope, port=None):
        previous = getattr(self._local, 'operation', None)
        self._local.operation = (scope, port)
        with self._state_lock:
            sequence = self._sequence
        try:
            yield
        except Exception as error:
            # Keep the SDK's concrete cause instead of a generic retry timeout.
            with self._state_lock:
                current = self._errors.get((scope, port), {}).get('_sequence', 0)
            if current <= sequence:
                self.report(scope, port, error)
            raise
        else:
            with self._state_lock:
                current = self._errors.get((scope, port), {}).get('_sequence', 0)
                if current <= sequence:
                    self._errors.pop((scope, port), None)
        finally:
            self._local.operation = previous
