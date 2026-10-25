"""下载并校验完整 CI 测试使用的模型、RAW 样片与相机配置。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def prepare(item):
    path = ROOT / item['path']
    if ROOT not in path.resolve().parents:
        raise ValueError(f"Invalid fixture path: {item['path']}")
    if path.is_file() and path.stat().st_size == item['size'] and sha256(path) == item['sha256']:
        print(f"Verified: {item['path']}", flush=True)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.part')
    if ROOT not in temporary.resolve().parents:
        raise ValueError(f"Invalid fixture download path: {temporary.name}")
    print(f"Downloading: {item['path']}", flush=True)
    request = Request(item['url'], headers={'User-Agent': 'OpenPictureStudio-CI'})
    with urlopen(request, timeout=120) as response, temporary.open('wb') as stream:
        for chunk in iter(lambda: response.read(1024 * 1024), b''):
            stream.write(chunk)
    if temporary.stat().st_size != item['size'] or sha256(temporary) != item['sha256']:
        raise ValueError(f"Fixture checksum mismatch: {item['path']}")
    temporary.replace(path)
    return path


def main():
    manifest = json.loads((ROOT / 'tests/fixtures/ci_assets.json').read_text(encoding='utf-8'))
    catalog = json.loads((ROOT / 'resources/models/catalog.json').read_text(encoding='utf-8'))
    models = {model['id']: model for model in catalog['models']}
    for model_id in manifest['model_ids']:
        for file in models[model_id]['files']:
            prepare(dict(file, path='models/' + file['name']))
    files = {item['id']: prepare(item) for item in manifest['files']}
    environment = {
        'OPS_RAW_FIXTURE': str(files['raw']),
        'OPS_CI_DCP_PROFILE': str(files['dcp']),
        'OPS_DCP_DUMP_DIR': str(ROOT / '.pytest-tmp/dcp-dump'),
    }
    output = ROOT / '.pytest-tmp/fixtures/environment.json'
    output.write_text(json.dumps(environment, indent=2) + '\n', encoding='utf-8')
    if os.environ.get('GITHUB_ENV'):
        with Path(os.environ['GITHUB_ENV']).open('a', encoding='utf-8') as stream:
            for name, value in environment.items():
                stream.write(f'{name}={value}\n')
    print(f'Fixture environment: {output}', flush=True)


if __name__ == '__main__':
    main()
