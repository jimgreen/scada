"""Serve isolated browser benchmark fixtures, without starting communication.

Run from the repository root. The baseline file is an explicit frozen app.js;
the current application is served normally, while app.before.js is exposed
only by this test server. No production database or log file is opened.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scdfe_c.web.seed_protocol_test_data import seed_protocol_test_data
from scdfe_c.web.server import ScadaHandler, make_server
from scdfe_c.web.service import ScadaStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-file', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=Path('output/playwright/hmi-round11'))
    parser.add_argument('--port', type=int, default=8881)
    args = parser.parse_args()
    baseline = args.baseline_file.read_bytes()
    root = args.root.resolve()
    repo = Path(__file__).resolve().parents[1]
    # Keep generated database/log data in the repository's disposable output.
    root.relative_to(repo / 'output')
    root.mkdir(parents=True, exist_ok=True)
    db = root / 'fixture.sqlite3'
    store = ScadaStore(db)
    seed_protocol_test_data(store, replicas=10)
    with store.connection() as connection:
        connection.execute('UPDATE fes_rtu_define SET process_enable=0')
    directory = root / 'logs' / '2026' / '11' / '02' / '10'
    directory.mkdir(parents=True, exist_ok=True)
    levels = ('日志', '警告', '错误', '进程')
    for rtu in range(1, 61):
        path = directory / f'scdfe_c-rtu-{rtu}-20261102-10.log'
        if not path.exists():
            path.write_text(''.join(
                f'10:{number // 60 % 60:02d}:{number % 60:02d} '
                f'[{levels[number % 4]}] 测点 {number} '
                f'采集完成 value={rtu + number / 10:.1f}\n'
                for number in range(5000)
            ), encoding='utf-8')

    class BenchmarkHandler(ScadaHandler):
        def _static(self, name):
            if name == 'app.before.js':
                return self._send(200, 'application/javascript; charset=utf-8', baseline)
            return super()._static(name)

    server = make_server('127.0.0.1', args.port, db_path=db)
    server.RequestHandlerClass = BenchmarkHandler
    print(f'HMI_BENCHMARK_READY http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
