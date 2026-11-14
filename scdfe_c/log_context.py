"""Small, cached configuration snapshots for communication lifecycle logs.

Only explicit diagnostic fields are selected; never dump a configuration row
or read point tables. Call once at startup, not from the collection loop.
"""
import json
from urllib.parse import urlsplit


_FIELDS = {
    'iec104': ('remote_host', 'remote_port', 'rtu_addr'),
    'modbus-tcp': ('remote_host', 'remote_port', 'unit_id'),
    'modbus-rtu': ('serial_port', 'baudrate', 'bytesize', 'parity', 'stopbits', 'unit_id'),
    'can': ('interface', 'channel', 'bitrate', 'data_bitrate', 'fd'),
    'bacnet': ('remote_host', 'remote_port', 'bind_ip', 'bind_port', 'dev_id'),
    'opc': ('opc_name',),
}


def _text(value):
    if value is None or value == '':
        return '未配置'
    # Keep one record per line, even for user-edited names/connection fields.
    return ' '.join(str(value).split())[:200]


def _address(host, port):
    host = _text(host)
    if ':' in host and not host.startswith('['):
        host = '[' + host + ']'
    return host + ':' + _text(port)


def channel_identity(spec):
    return (f'RTU编号={spec.rtu_id} RTU名称={json.dumps(_text(spec.name), ensure_ascii=False)} '
            f'通道类型={spec.channel_type} 通道编号={spec.channel_id}')


def channel_context(store, spec):
    identity = channel_identity(spec)
    fields = _FIELDS.get(spec.channel_type)
    if store is None or fields is None:
        return identity
    # The identifiers are from the allowlist, never supplied by a log message.
    table = 'fes_channel_' + spec.channel_type.replace('-', '_') + '_define'
    try:
        with store.connection() as connection:
            row = connection.execute(
                f'SELECT {", ".join(fields)} FROM {table} WHERE channel_id=?',
                (spec.channel_id,),
            ).fetchone()
        if row is None:
            return identity + ' 连接参数=未配置'
        params = dict(row)
    except Exception as exc:  # Diagnostics must not prevent channel startup.
        return identity + f' 连接参数=读取失败({type(exc).__name__})'
    text = lambda key: _text(params.get(key))
    kind = spec.channel_type
    if kind in ('iec104', 'modbus-tcp', 'bacnet'):
        detail = '目标=' + _address(params['remote_host'], params['remote_port'])
        if kind == 'iec104':
            detail += ' 公共地址CA=' + text('rtu_addr')
        elif kind == 'modbus-tcp':
            detail += ' 站号=' + text('unit_id')
        else:
            detail += (' 本端=' + _address(params['bind_ip'], params['bind_port'])
                       + ' 设备ID=' + text('dev_id'))
    elif kind == 'modbus-rtu':
        detail = (f'串口={text("serial_port")} 串口参数={text("baudrate")}/'
                  f'{text("bytesize")}{text("parity")}{text("stopbits")} 站号={text("unit_id")}')
    elif kind == 'can':
        detail = (f'CAN接口={text("interface")}/{text("channel")} 仲裁波特率={text("bitrate")} '
                  f'CAN-FD={text("fd")} 数据波特率={text("data_bitrate")}')
    else:
        # A URL may embed credentials; record only scheme, host and port.
        try:
            url = urlsplit(params['opc_name'] or '')
            if not url.scheme or not url.hostname:
                raise ValueError('invalid endpoint')
            host = url.hostname
            if ':' in host:
                host = '[' + host + ']'
            detail = f'端点={url.scheme}://{host}' + (f':{url.port}' if url.port else '')
        except (ValueError, TypeError):
            detail = '端点=未配置或无效'
    return identity + ' ' + detail
