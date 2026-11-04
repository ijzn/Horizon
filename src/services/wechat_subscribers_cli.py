"""Manage independently bound WeChat readers and manually deliver one briefing."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import zlib

from dotenv import load_dotenv
import qrcode

from ..models import WeChatConfig
from ..storage.manager import StorageManager
from . import wechat_cli
from .wechat import WeChatNotifier, plan_chunks
from .wechat_subscribers import SubscriberStore


def save_qr(content: str, directory: Path) -> Path:
    """Write a private, scannable PNG without Pillow or printing its login URL."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    qr = qrcode.QRCode(border=4)
    qr.add_data(content)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    scale = 8
    width = len(matrix) * scale
    rows = []
    for row in matrix:
        pixels = bytes(v for cell in row for v in [0 if cell else 255] * scale)
        rows.extend([b'\0' + pixels] * scale)

    def chunk(kind, body):
        return struct.pack('!I', len(body)) + kind + body + struct.pack('!I', zlib.crc32(kind + body) & 0xffffffff)

    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', width, width, 8, 0, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b'')
    path = directory / f'login-{time.time_ns()}.png'
    fd, temporary = tempfile.mkstemp(prefix='.qr-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(png)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f'微信二维码：{path.resolve()}', flush=True)
    print('请让这位朋友用自己的微信扫码，确认后给机器人发送“开始日报”。', flush=True)
    return path


async def login_reader(store, reader_id, config, force=False):
    reader = store.get(reader_id)
    if not reader.enabled:
        raise ValueError('该订阅者已停用，请先 enable。')
    storage = store.storage_for(reader_id)
    async with store.binding_guard(reader_id):
        original = wechat_cli._render_qr
        wechat_cli._render_qr = lambda content: save_qr(content, Path(storage.data_dir))
        try:
            await wechat_cli._run_login(config, storage, force)
        finally:
            wechat_cli._render_qr = original


def configured_wechat(data_dir, config_path):
    storage = StorageManager(str(data_dir), config_path=config_path)
    config = storage.load_config()
    return config.wechat or WeChatConfig()


async def run(args):
    store = SubscriberStore(args.data_dir)
    if args.command == 'list':
        if args.json:
            print(json.dumps(store.describe(), ensure_ascii=False, indent=2))
        else:
            for row in store.describe():
                state = '已绑定' if row.get('ready') else ('待首次消息' if row.get('session_present') else '待扫码')
                if not row['enabled']:
                    state = '已停用'
                print(f"{row['id']}\t{row['name']}\t{state}")
        return 0
    if args.command == 'add':
        reader = store.add(args.name)
        print(f'已添加：{reader.name}，ID：{reader.id}')
        if args.login:
            await login_reader(store, reader.id, configured_wechat(args.data_dir, args.config))
        return 0
    if args.command in ('enable', 'disable', 'remove'):
        if args.command == 'remove':
            store.remove(args.reader_id)
        else:
            store.set_enabled(args.reader_id, args.command == 'enable')
        print(f'{args.reader_id}: {args.command}；会话和历史记录保留。')
        return 0
    if args.command == 'login':
        await login_reader(store, args.reader_id, configured_wechat(args.data_dir, args.config), args.force)
        return 0
    if args.command == 'status':
        # Validate through the store; no tokens or platform user IDs are printed.
        store.get(args.reader_id)
        if args.refresh:
            config = configured_wechat(args.data_dir, args.config)
            async with store.binding_guard(args.reader_id):
                notifier = WeChatNotifier(config, store.storage_for(args.reader_id))
                await notifier.refresh_context()
        print(json.dumps([r for r in store.describe() if r['id'] == args.reader_id], ensure_ascii=False, indent=2))
        return 0
    if args.command in ('send', 'test'):
        if args.command == 'send':
            summary = Path(args.file).read_text(encoding='utf-8')
        else:
            summary = '# Horizon 多人微信推送测试\n\n这是一条测试消息，用于确认你已独立订阅日报。\n'
        if not summary.strip():
            raise ValueError('内容为空，未发送。')
        selected = args.to
        if selected:
            readers = [store.get(reader_id) for reader_id in selected]
        else:
            readers = [reader for reader in store.list() if reader.enabled]
        if args.dry_run:
            # This path does not load credentials, build a notifier or require an AI config.
            print('预览收件人：' + ', '.join(f'{r.name} ({r.id})' for r in readers))
            for index, text in enumerate(plan_chunks(summary, 4000, args.lang), 1):
                print(f'\n消息 {index}\n{text}')
            print('预览完成，未发送消息。')
            return 0
        config = configured_wechat(args.data_dir, args.config)
        if not config.enabled:
            raise ValueError('wechat.enabled 未开启，未发送。')
        results = await store.broadcast(summary, config, subscriber_ids=selected, lang=args.lang, force=args.force)
        if not results:
            print('没有可投递的订阅者。')
            return 1
        for result in results:
            print(f'{result.name} ({result.id}): {result.status}')
            if result.warning:
                print(f'  {result.warning}')
        return 1 if any(result.status == 'failed' for result in results) else 0
    raise ValueError('未知命令')


def main():
    parser = argparse.ArgumentParser(description='多人微信订阅：独立扫码绑定，手动投递同一份日报')
    parser.add_argument('-d', '--data-dir', default='data')
    parser.add_argument('-c', '--config', default=None)
    sub = parser.add_subparsers(dest='command', required=True)
    listing = sub.add_parser('list', help='查看订阅者和脱敏绑定状态')
    listing.add_argument('--json', action='store_true')
    add = sub.add_parser('add', help='添加一位朋友，不影响现有绑定')
    add.add_argument('name')
    add.add_argument('--login', action='store_true', help='立即生成专属登录二维码')
    login = sub.add_parser('login', help='为指定订阅者生成专属二维码')
    login.add_argument('reader_id')
    login.add_argument('--force', action='store_true', help='显式重新绑定这一位订阅者')
    for command in ('enable', 'disable', 'remove'):
        sub.add_parser(command).add_argument('reader_id')
    status = sub.add_parser('status', help='查看指定订阅者状态，不显示凭据')
    status.add_argument('reader_id')
    status.add_argument('--refresh', action='store_true')
    for command in ('send', 'test'):
        send = sub.add_parser(command, help='手动推送已有日报' if command == 'send' else '手动发送订阅测试')
        if command == 'send':
            send.add_argument('--file', required=True)
        send.add_argument('--to', action='append', help='指定收件人ID，可多次传入；省略则全部启用者')
        send.add_argument('--lang', default='zh')
        send.add_argument('--dry-run', action='store_true')
        send.add_argument('--force', action='store_true', help='显式重发相同内容')
    args = parser.parse_args()
    load_dotenv()
    try:
        code = asyncio.run(run(args))
    except Exception as exc:
        # Do not echo arbitrary exception content: network/config errors can contain secrets.
        print(f'操作失败（{type(exc).__name__}）。请检查收件人ID、文件和配置；未显示凭据。', file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        print('已取消，其他订阅者不受影响。', file=sys.stderr)
        code = 130
    if code:
        raise SystemExit(code)


if __name__ == '__main__':
    main()
