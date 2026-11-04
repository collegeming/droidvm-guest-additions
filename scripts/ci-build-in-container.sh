#!/bin/bash
# Runs INSIDE an aarch64 Arch Linux ARM container (see
# .github/workflows/build-virtio-gpu-module.yml). Building on the guest's own
# architecture is what makes this a meaningful gate: the guest kernel's prepared
# tree carries native kbuild host tools, and cross-building it from x86 fails on
# exec-format and on compiler-version-specific flags.
set -euxo pipefail

pacman -Sy --noconfirm --needed base-devel python linux-aarch64-headers

python tests/test_pool_mmap_bounds.py
python scripts/patch_pool_mmap_bounds.py --root .

KDIR=$(ls -d /usr/lib/modules/*/build | head -1)
echo "KDIR=$KDIR"
cat "$KDIR/include/config/kernel.release"

make -C gunyah_guest KDIR="$KDIR" -j"$(nproc)"
make -C virtio_gpu KDIR="$KDIR" -j"$(nproc)" \
     KBUILD_EXTRA_SYMBOLS="$PWD/gunyah_guest/Module.symvers"

ls -l virtio_gpu/*.ko gunyah_guest/*.ko
