#!/usr/bin/env python3
"""Self-test for scripts/patch_pool_mmap_bounds.py.

Runs against a copy of a source tree (default: this repo's virtio_gpu/), in a
temp dir, so the working tree is never touched. Checks:

  1. every anchor matches exactly once on a pristine tree,
  2. the patched files contain the invariants the audit relies on,
  3. a second run is a no-op (idempotent),
  4. --check writes nothing.

    python tests/test_pool_mmap_bounds.py [--source <tree with virtio_gpu/>]
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / 'scripts' / 'patch_pool_mmap_bounds.py'

INVARIANTS = {
    'virtio_gpu/virtgpu_drv.h': [
        'phys_addr_t gpu_pool_size;',
        'u64 blob_size;',
    ],
    'virtio_gpu/virtgpu_kms.c': [
        'phys_addr_t *size_out',
        '*size_out = resource_size(&res);',
        'virtio_gpu_find_pool_base(&pool_node',
    ],
    'virtio_gpu/virtgpu_vram.c': [
        'vram->blob_size ? vram->blob_size : vram->vram_node.size',
        'pool mmap out of range',
        'virtio-gpu: pool mmap res=',
        'vram->blob_size = params->size;',
        'refusing dma-buf export of pool-resident blob',
    ],
}


def run(root, *extra):
    return subprocess.run([sys.executable, str(SCRIPT), '--root', str(root), *extra],
                          capture_output=True, text=True)


def brace_balance(path):
    text = path.read_text(encoding='utf-8')
    # Strip line comments and string literals: good enough for these files.
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    text = re.sub(r'//[^\n]*', '', text)
    text = re.sub(r'"(\\.|[^"\\])*"', '""', text)
    return text.count('{') - text.count('}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', default=str(HERE.parent))
    args = ap.parse_args()

    src = Path(args.source)
    if not (src / 'virtio_gpu' / 'virtgpu_vram.c').is_file():
        raise SystemExit(f'no virtio_gpu/ under {src}')

    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / 'tree'
        shutil.copytree(src / 'virtio_gpu', work / 'virtio_gpu')

        # 4. --check writes nothing
        before = (work / 'virtio_gpu' / 'virtgpu_vram.c').read_text(encoding='utf-8')
        r = run(work, '--check')
        assert r.returncode == 0, r.stderr
        assert 'check only' in r.stdout, r.stdout
        after = (work / 'virtio_gpu' / 'virtgpu_vram.c').read_text(encoding='utf-8')
        assert before == after, '--check modified the tree'
        print('ok: --check is read-only')

        # 1./2. patch + invariants
        r = run(work)
        assert r.returncode == 0, r.stderr
        assert 'anchor not unique' not in r.stdout + r.stderr
        for rel, needles in INVARIANTS.items():
            text = (work / rel).read_text(encoding='utf-8')
            for needle in needles:
                assert needle in text, f'{rel}: missing {needle!r}'
            bal = brace_balance(work / rel)
            assert bal == 0, f'{rel}: braces unbalanced ({bal})'
        print('ok: anchors applied, invariants present, braces balanced')

        # 3. idempotent
        r = run(work)
        assert r.returncode == 0, r.stderr
        assert r.stdout.count('already applied') == 3, r.stdout
        print('ok: second run is a no-op')

    print('PASS')


if __name__ == '__main__':
    main()
