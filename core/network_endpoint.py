"""Network addresses for HMI monitors; serial/CAN transports have no IP endpoint."""
from urllib.parse import urlsplit
import sqlite3


def network_endpoint(protocol, config, *, server=False):
    protocol = str(protocol or "").removesuffix("-server")
    role, transport = "listen" if server else "remote", "TCP"
    if protocol == "modbus-tcp":
        host, port = ((config.get("bind_ip"), config.get("listen_port")) if server else
                      (config.get("remote_host"), config.get("remote_port", 502)))
    elif protocol == "iec104":
        host, port = ((config.get("bind_ip"), config.get("port")) if server else
                      (config.get("remote_host"), config.get("remote_port", 2404)))
    elif protocol == "bacnet":
        transport = "UDP"
        host, port = ((config.get("bind_ip"), config.get("bind_port")) if server else
                      (config.get("remote_host"), config.get("remote_port", 47808)))
    elif protocol == "opc":
        if server:
            host, port = config.get("bind_ip"), config.get("port")
        else:
            try:
                url = urlsplit(config.get("opc_name") or "")
                if url.scheme not in {"opc.tcp", "opc.https", "https"}:
                    return None
                host, port = url.hostname, url.port or (4840 if url.scheme == "opc.tcp" else 443)
            except ValueError:
                return None
    else:
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    host = str(host or "").strip()
    if not host or not 1 <= port <= 65535:
        return None
    return {"host": host, "port": port, "role": role, "transport": transport}


def configured_endpoints(store, rtus):
    """Read only this page's channel definitions, at most one query per protocol.

    Addresses remain persisted in their authoritative channel tables. A stopped
    or failed worker therefore still has an address, without writes on UI reads.
    """
    sources = {
        "iec104": ("fes_channel_iec104_define", "remote_host,remote_port"),
        "modbus-tcp": ("fes_channel_modbus_tcp_define", "remote_host,remote_port"),
        "bacnet": ("fes_channel_bacnet_define", "remote_host,remote_port"),
        "opc": ("fes_channel_opc_define", "opc_name"),
    }
    groups = {}
    for row in rtus:
        protocol, channel_id = row.get("channel_type"), row.get("channel_id")
        if protocol in sources and channel_id is not None:
            groups.setdefault(protocol, set()).add(int(channel_id))
    result = {}
    if not groups:
        return result
    with store.connection() as con:
        size = max(1, con.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
        for protocol, ids in groups.items():
            table, fields = sources[protocol]
            ids = sorted(ids)
            for offset in range(0, len(ids), size):
                batch = ids[offset:offset + size]
                sql = f"SELECT channel_id,{fields} FROM {table} WHERE channel_id IN ({','.join('?' for _ in batch)})"
                for row in con.execute(sql, batch):
                    result[(protocol, int(row["channel_id"]))] = network_endpoint(protocol, dict(row))
    return result
