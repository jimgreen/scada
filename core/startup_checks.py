"""Actionable configuration checks. Never connect to a field device."""
import socket
import ipaddress


def listener_conflicts(rows):
    issues = []
    for i, a in enumerate(rows):
        for b in rows[i+1:]:
            if a.get('bind_port') != b.get('bind_port'):
                continue
            ah,bh = str(a.get('bind_host') or ''),str(b.get('bind_host') or '')
            if ah == bh or '0.0.0.0' in (ah,bh) or '::' in (ah,bh):
                ids = [a['channel_id'],b['channel_id']]
                issues.append(dict(code='configured_listener_conflict',severity='warning',channels=ids,
                    message=f'通道 {ids[0]} 与 {ids[1]} 的监听地址重叠（端口 {a["bind_port"]}），同时启动可能冲突'))
    return issues


def check_listener(host, port):
    """Check local bind availability without creating a client connection.

    This is advisory until the real server binds; the OS remains the final
    authority. Call before startup, never against one's own active listener.
    """
    try:
        addresses = socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
        family, socktype, proto, _, address = addresses[0]
        with socket.socket(family, socktype, proto) as sock:
            if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
                sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            else:
                sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(address)
        return None
    except (OSError, ValueError, IndexError) as exc:
        return dict(code='listener_unavailable',severity='error',
                    message=f'无法监听 {host}:{port}：地址或端口不可用；请检查端口占用和本机地址（{exc}）')


def configuration_checks(store, component, services=None):
    from core.network_endpoint import configured_endpoints, network_endpoint
    issues = []
    if component == 't':
        with store.connection() as con:
            rows = [dict(row) for row in con.execute('''SELECT channel_id,name,bind_host,bind_port,
                process_status,process_heartbeat_time FROM fes_scdfe_t_define ORDER BY channel_id''')]
        issues.extend(listener_conflicts(rows))
        for row in rows:
            if row['process_status'] != 2:
                issue = check_listener(row['bind_host'],row['bind_port'])
                if issue:
                    issues.append(dict(issue,channels=[row['channel_id']],name=row['name']))
    elif component == 's':
        with store.connection() as con:
            rtus={row[0]:(row[1],row[2]) for row in con.execute('SELECT rtu_id,channel_id,name FROM fes_rtu_define')}
        for slug,service in (services or {}).items():
            config=service.config()
            rid=config.get('effective_rtu_id') or config.get('rtu_id')
            cid,name=rtus.get(rid,(None,slug))
            context=dict(channels=[cid] if cid is not None else [],name=name,rtu_id=rid,service=slug)
            if config.get('simulation'):
                issues.append(dict(context,code='local_simulation',severity='warning',
                    message='本地模拟已启用；不能据此确认实际串口或网络对端通信正常'))
                continue
            protocol=getattr(service,'PROTOCOL','iec104')
            server=config.get('mode','server')=='server'
            endpoint=network_endpoint(protocol,config,server=server)
            if server and endpoint and endpoint['transport']=='TCP' and not service.running:
                issue=check_listener(endpoint['host'],endpoint['port'])
                if issue:issues.append(dict(issue,**context))
    else:
        with store.connection() as con:
            rtus=[dict(row) for row in con.execute('SELECT rtu_id,name,channel_type,channel_id FROM fes_rtu_define')]
            serial={row[0]:row[1] for row in con.execute('SELECT channel_id,serial_port FROM fes_channel_modbus_rtu_define')}
        endpoints=configured_endpoints(store,rtus)
        for row in rtus:
            context=dict(channels=[row['channel_id']],rtu_id=row['rtu_id'],name=row['name'])
            if row['channel_type']=='modbus-rtu' and str(serial.get(row['channel_id'],'')).startswith('loop://'):
                issues.append(dict(context,code='serial_loopback',severity='warning',
                    message='loop:// 是本端串口回环，不能作为 C/S 两进程的配对串口链路'))
            endpoint=endpoints.get((row['channel_type'],row['channel_id']))
            if endpoint and endpoint['transport']=='TCP':
                try:local=ipaddress.ip_address(endpoint['host']).is_loopback
                except ValueError:local=endpoint['host']=='localhost'
                if local and check_listener(endpoint['host'],endpoint['port']) is None:
                    issues.append(dict(context,code='local_peer_not_listening',severity='warning',
                        message=f'本机目标 {endpoint["host"]}:{endpoint["port"]} 端口可绑定，请确认对端服务已经启动'))
    return issues
